import argparse
import importlib.util
import json
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

from rca_lab.mcp.golden import ExpectedCase
from rca_lab.mcp.hint_ladder import exclusion_hint, pick_verdicts, rung_hint

A, A2, B, C, D = (f"{n}0000000-0000-0000-0000-000000000000" for n in "abcde")
ONE_ROOT = ExpectedCase.model_validate({"expected_status": "confirmed", "roots": [{"target_ids": [A, A2]}]})
TWO_ROOTS = ExpectedCase.model_validate({"expected_status": "provisional", "roots": [{"target_ids": [A]}, {"target_ids": [D]}]})


def answer(*targets):
    return {"status": "provisional", "summary": "s", "causes": [{"target": t, "mechanism": "m", "support_refs": []} for t in targets]}


def test_pick_verdicts_counts_family_ids_as_right():
    wrong, matched, missed = pick_verdicts(ONE_ROOT, answer(A2, B))
    assert [c.target_id for c in wrong] == [B] and matched == 1 and missed == 0


def test_exclusion_hint_names_only_wrong_picks_and_flags_incomplete():
    names = {B: "commerce-cart", C: "PostgreSQL-commerce"}
    text = exclusion_hint([answer(B), answer(B, C), answer(A)], TWO_ROOTS, names)
    assert "commerce-cart, PostgreSQL-commerce" in text  # most frequent first
    assert "놓친 근본 원인" in text
    assert A not in text and D not in text  # never the answer
    assert exclusion_hint([None], ONE_ROOT, {}) == "이전 조사는 근본 원인을 맞히지 못했다."


def test_rung_hint_ladder_shape():
    assert rung_hint(0, results=[answer(B)], expected=ONE_ROOT, names={}, written="w") is None
    assert "오답" in rung_hint(1, results=[answer(B)], expected=ONE_ROOT, names={}, written=None)
    assert rung_hint(2, results=[answer(B)], expected=ONE_ROOT, names={}, written=None) is None
    assert rung_hint(2, results=[answer(B)], expected=ONE_ROOT, names={}, written="write-up").endswith("write-up")


def _load_runner():
    spec = importlib.util.spec_from_file_location("mcp_case_run_under_test", Path("scripts/mcp_case_run.py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _fake_env(monkeypatch, runner, solve_when):
    seen_hints = []

    @contextmanager
    def fake_client(*_a, **_k):
        yield SimpleNamespace(list_tools=lambda: [{"name": "probe_metric", "description": "Metric series. More text."}])

    def fake_student(config, seed, mcp):
        seen_hints.append(config.hint)
        target = A if solve_when(config.hint) else B
        return SimpleNamespace(
            messages=[{"role": "system", "content": "clean system"}, {"role": "user", "content": "u"}],
            alias_map={}, observed_refs=set(), prompt_tokens_last=1, stop_reason="submitted", wall_seconds=1.0,
            result=answer(target), turns=3, tool_calls=2, tool_errors=0,
        )

    monkeypatch.setattr(runner, "restore_case", lambda *a, **k: {"RCA_PG_DSN": "x"})
    monkeypatch.setattr(runner, "teardown", lambda project: None)
    monkeypatch.setattr(runner, "build_observed_seed", lambda *a, **k: {"time_window": {"first_event": "f", "last_event": "l"}})
    monkeypatch.setattr(runner, "blind_seed", lambda binary, seed: seed)
    monkeypatch.setattr(runner, "MCPClient", fake_client)
    monkeypatch.setattr(runner, "run_student", fake_student)
    monkeypatch.setattr(runner, "target_names", lambda project, ids: {B: "commerce-cart"})
    return seen_hints


def _args(tmp_path, **over):
    base = dict(  # noqa: C408 — mirrors argparse field names
        runner="student", cases_root=str(tmp_path), project_prefix="t", overwrite=False, reuse_db=False, keep_db=False, max_alarms=50,
                endpoint="e", model="m", no_alias=False, no_guided=False, max_turns=5, max_prompt_tokens=1000, tool_result_max_chars=100,
                temperature=1.0, top_p=1.0, top_k=1, seed=0, runs=2, hints=None, hints_by_case={}, hinted_only=False,
                hint_ladder=True, ladder_hints_by_case={"case-x-v3-1": "written guidance"}, ladder_max_rung=2, contract="c",
        ladder_writer="file", ladder_writer_model="m", ladder_stop="target", ladder_prior_root=None, no_reasoning_guard=False)
    base.update(over)
    return argparse.Namespace(**base)


def test_ladder_stops_at_first_full_hit_and_keeps_hints_out_of_records(tmp_path, monkeypatch):
    runner = _load_runner()
    seen = _fake_env(monkeypatch, runner, solve_when=lambda hint: bool(hint and "commerce-cart" in hint))
    rows = runner.run_case(_args(tmp_path), "case-x-v3-1", {"case-x-v3-1": ONE_ROOT}, tmp_path / "out", Path("/bin/true"))
    assert [row["hint_rung"] for row in rows] == [0, 0, 1, 1]
    assert seen[:2] == [None, None] and "commerce-cart" in seen[2] and "written guidance" not in seen[2]
    ladder = json.loads((tmp_path / "out/case-x-v3-1/ladder.json").read_text())
    assert ladder["solved_at_rung"] == 1 and ladder["done"] is True
    for run in (tmp_path / "out/case-x-v3-1").glob("run*"):
        assert "commerce-cart" not in (run / "trajectory.jsonl").read_text()
    assert json.loads((tmp_path / "out/case-x-v3-1/run3/score.json").read_text())["hinted"] is True


def test_ladder_reaches_written_hint_and_plain_path_unchanged(tmp_path, monkeypatch):
    runner = _load_runner()
    seen = _fake_env(monkeypatch, runner, solve_when=lambda hint: bool(hint and "written guidance" in hint))
    rows = runner.run_case(_args(tmp_path), "case-x-v3-1", {"case-x-v3-1": ONE_ROOT}, tmp_path / "out", Path("/bin/true"))
    assert [row["hint_rung"] for row in rows] == [0, 0, 1, 1, 2, 2] and rows[-1]["root_f1"] == 1.0
    assert seen[4].startswith("이전 조사 판정: commerce-cart")

    seen.clear()
    rows = runner.run_case(_args(tmp_path, hint_ladder=False), "case-x-v3-1", {"case-x-v3-1": ONE_ROOT}, tmp_path / "plain", Path("/bin/true"))
    assert len(rows) == 2 and seen == [None, None] and "hint_rung" not in rows[0]


def test_claude_hint_rejects_named_root_and_audited_leak_then_accepts():
    from rca_lab.mcp.hint_ladder import claude_hint

    replies = iter([
        ('{"hint": "commerce-inventory 를 보라"}', ""),  # names a root -> rejected without an audit call
        ('{"hint": "재고 쪽 CPU 제한이 원인이다"}', ""), ('{"leak": true, "reason": "states the mechanism"}', ""),
        ('{"hint": "동기 경로 대신 파드 단위 자원 지표를 창 전후로 비교하라"}', ""), ('{"leak": false, "reason": "direction only"}', ""),
    ])
    prompts = []

    def fake(**kw):
        prompts.append(kw["prompt"])
        return next(replies)

    hint, audit = claude_hint(rung=2, reference={"cause": "x"}, forbidden=["commerce-inventory"], attempts=[], given=[], tools=[], model="m", run_claude=fake)
    assert hint.startswith("동기 경로") and [a["stage"] for a in audit] == ["name_check", "leak_audit", "leak_audit"]
    assert "states the mechanism" in prompts[3]  # the rejection reason goes back to the writer


def test_ladder_with_claude_writer_climbs_to_navigation(tmp_path, monkeypatch):
    runner = _load_runner()
    seen = _fake_env(monkeypatch, runner, solve_when=lambda hint: bool(hint and "NAV" in hint))
    written = []

    def fake_claude_hint(**kw):
        written.append(kw)
        return ("DIR hint" if kw["rung"] == 2 else "NAV hint"), [{"stage": "leak_audit", "leak": False}]

    monkeypatch.setattr(runner, "claude_hint", fake_claude_hint)
    monkeypatch.setattr(runner, "scenario_reference", lambda case_dir: {"cause": "ref"})
    rows = runner.run_case(_args(tmp_path, ladder_writer="claude", ladder_max_rung=3), "case-x-v3-1", {"case-x-v3-1": ONE_ROOT}, tmp_path / "out", Path("/bin/true"))
    assert [row["hint_rung"] for row in rows] == [0, 0, 1, 1, 2, 2, 3, 3]
    assert seen[4] == "이전 조사 판정: commerce-cart 을(를) 근본 원인으로 지목한 결론은 오답 판정.\nDIR hint"
    assert [w["rung"] for w in written] == [2, 3] and written[0]["tools"] == ["probe_metric: Metric series"]
    assert len(written[1]["attempts"]) == 6 and "tool_calls" in written[1]["attempts"][-1] and "tool_calls" not in written[1]["attempts"][0]
    assert len(written[1]["given"]) == 2  # rung 1 and rung 2 hints
    ladder = json.loads((tmp_path / "out/case-x-v3-1/ladder.json").read_text())
    assert [step["writer"] for step in ladder["steps"]] == ["none", "auto", "claude", "claude"] and ladder["solved_at_rung"] == 3


def test_forbidden_root_names_use_short_k8s_names():
    from rca_lab.mcp.hint_ladder import forbidden_root_names

    k = "a072cd3d-4a33-4542-832d-b4beec88609f"
    names = {A: "commerce-order", A2: f"{k}:deployment:rca-testbed-commerce/testbed-order",
             B: f"{k}:container:rca-testbed-commerce/testbed-order-5c4d-bkk/order-service", C: f"{k}:replicaset:rca-testbed-commerce/testbed-order-5c4d"}
    expected = ExpectedCase.model_validate({"expected_status": "confirmed", "roots": [{"target_ids": [A, A2, B, C]}]})
    assert forbidden_root_names(expected, names) == ["commerce-order", "order-service", "testbed-order"]


def test_rung1_skipped_when_blind_runs_named_nothing(tmp_path, monkeypatch):
    runner = _load_runner()
    seen = _fake_env(monkeypatch, runner, solve_when=lambda hint: bool(hint and "DIR" in hint))

    def no_pick_student(config, seed, mcp, _inner=runner.run_student):
        episode = _inner(config, seed, mcp)
        if not config.hint:
            episode.result = {"status": "insufficient", "summary": "s", "causes": []}
        return episode

    monkeypatch.setattr(runner, "run_student", no_pick_student)
    monkeypatch.setattr(runner, "claude_hint", lambda **kw: ("DIR hint", []))
    monkeypatch.setattr(runner, "scenario_reference", lambda case_dir: {"cause": "ref"})
    rows = runner.run_case(_args(tmp_path, ladder_writer="claude", ladder_max_rung=3), "case-x-v3-1", {"case-x-v3-1": ONE_ROOT}, tmp_path / "out", Path("/bin/true"))
    assert [row["hint_rung"] for row in rows] == [0, 0, 2, 2]
    assert seen[2].startswith("이전 조사는 근본 원인을 맞히지 못했다.")


def test_mechanism_stop_climbs_past_a_target_only_hit(tmp_path, monkeypatch):
    runner = _load_runner()
    _fake_env(monkeypatch, runner, solve_when=lambda hint: bool(hint))  # every hinted run names the root
    grades = iter([0.5, 0.5, 1.0, 0.5])  # both runs of a rung are graded before the stop check
    monkeypatch.setattr(runner, "judge_mechanism", lambda *a, **k: (next(grades), "d"))
    monkeypatch.setattr(runner, "reference_cause", lambda case_dir: "ref")
    monkeypatch.setattr(runner, "claude_hint", lambda **kw: ("DIR hint", []))
    monkeypatch.setattr(runner, "scenario_reference", lambda case_dir: {"cause": "ref"})
    rows = runner.run_case(_args(tmp_path, ladder_writer="claude", ladder_max_rung=3, ladder_stop="mechanism"), "case-x-v3-1", {"case-x-v3-1": ONE_ROOT}, tmp_path / "out", Path("/bin/true"))
    assert [row["hint_rung"] for row in rows] == [0, 0, 1, 1, 2, 2]
    ladder = json.loads((tmp_path / "out/case-x-v3-1/ladder.json").read_text())
    assert ladder["solved_at_rung"] == 2 and [r["mechanism"] for r in ladder["steps"][2]["runs"]] == [1.0, 0.5]
    assert [r["mechanism"] for r in ladder["steps"][1]["runs"]] == [0.5, 0.5]  # target-only hits did not stop rung 1
    cached = json.loads((tmp_path / "out/case-x-v3-1/run5/mechanism_judge.json").read_text())
    assert cached["rubric"] == "v2" and cached["score"] == 1.0


def test_exclusion_hint_flags_right_target_with_failed_mechanism():
    text = exclusion_hint([answer(A)], ONE_ROOT, {})
    assert "대상 지목이 맞은 결론도" in text and "맞히지 못했다" not in text and A not in text


def test_ladder_starts_at_rung1_from_prior_rollouts(tmp_path, monkeypatch):
    runner = _load_runner()
    seen = _fake_env(monkeypatch, runner, solve_when=lambda hint: bool(hint and "commerce-cart" in hint))
    prior = tmp_path / "rl-iter" / "case-x-v3-1"
    for index in (1, 2, 3):
        (prior / f"run{index}").mkdir(parents=True)
        (prior / f"run{index}" / "score.json").write_text(json.dumps({"root_f1": 0.0, "status": "provisional", "stop_reason": "submitted"}))
        (prior / f"run{index}" / "result.json").write_text(json.dumps(answer(B)))
    rows = runner.run_case(_args(tmp_path, ladder_prior_root=str(tmp_path / "rl-iter")), "case-x-v3-1", {"case-x-v3-1": ONE_ROOT}, tmp_path / "out", Path("/bin/true"))
    assert [row["hint_rung"] for row in rows] == [1, 1] and "commerce-cart" in seen[0]
    ladder = json.loads((tmp_path / "out/case-x-v3-1/ladder.json").read_text())
    assert ladder["steps"][0]["writer"] == "prior" and len(ladder["steps"][0]["runs"]) == 3 and ladder["solved_at_rung"] == 1
    assert not (tmp_path / "out/case-x-v3-1/run3").exists()  # new runs are numbered from 1


def test_exclusion_hint_never_rejects_external_picks():
    with_external = {**answer(B), "external_causes": [{"id": "external:k6", "name": "upstream request surge", "kind": "external_dependency", "boundary_target": C, "mechanism": "m"}]}
    text = exclusion_hint([with_external], ONE_ROOT, {B: "commerce-cart"})
    assert "commerce-cart" in text and "surge" not in text
    only_external = {"status": "provisional", "summary": "s", "causes": [], "external_causes": with_external["external_causes"]}
    assert exclusion_hint([only_external], ONE_ROOT, {}) == "이전 조사는 근본 원인을 맞히지 못했다."


def test_teacher_tool_calls_come_from_the_cli_stream(tmp_path):
    from rca_lab.mcp.hint_ladder import attempt_digest

    events = [
        {"type": "system", "subtype": "init"},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "hmm"}, {"type": "tool_use", "name": "mcp__rca-tools__describe_target", "input": {"target": B}}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "content": "ok"}]}},
        "not json",
    ]
    (tmp_path / "stream.jsonl").write_text("\n".join(e if isinstance(e, str) else json.dumps(e) for e in events))
    digest = attempt_digest(tmp_path, answer(B), {B: "commerce-cart"}, with_calls=True)
    assert digest["tool_calls"] == ['describe_target({"target": "commerce-cart"})'] and digest["causes"][0]["target"] == "commerce-cart"


def test_teacher_ladder_uses_prior_runs_and_passes_the_hint(tmp_path, monkeypatch):
    runner = _load_runner()
    _fake_env(monkeypatch, runner, solve_when=lambda hint: False)
    hints = []

    def fake_teacher(config, seed, connections, run_dir):
        hints.append(config.hint)
        target = A if config.hint and "commerce-cart" in config.hint else B
        return SimpleNamespace(stderr_tail="", result=answer(target), stop_reason="submitted", wall_seconds=1.0, turns=3, tool_calls=2, tool_errors=0, observed_refs=set())

    monkeypatch.setattr(runner, "run_teacher", fake_teacher)
    prior = tmp_path / "teacher-v4" / "case-x-v3-1"
    for index in (1, 2):
        (prior / f"run{index}").mkdir(parents=True)
        (prior / f"run{index}" / "score.json").write_text(json.dumps({"root_f1": 0.0, "status": "provisional", "stop_reason": "submitted"}))
        (prior / f"run{index}" / "result.json").write_text(json.dumps(answer(B)))
    args = _args(tmp_path, runner="teacher", ladder_prior_root=str(tmp_path / "teacher-v4"), claude_model="claude-opus-5-5", mcp_binary=None)
    rows = runner.run_case(args, "case-x-v3-1", {"case-x-v3-1": ONE_ROOT}, tmp_path / "out", Path("/bin/true"))
    assert [row["hint_rung"] for row in rows] == [1, 1] and rows[0]["runner"] == "teacher" and rows[0]["hinted"] is True
    assert all("commerce-cart" in h for h in hints)
