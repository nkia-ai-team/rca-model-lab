import json
import subprocess
import sys
from pathlib import Path

from rca_lab.mcp.hinted_data import ladder_verdict, load_rationalized

CASE = "case-x-v3-1"
GOOD = {"hinted": True, "stop_reason": "submitted", "root_f1": 1.0, "ref_grounding": 1.0, "case_id": CASE}
CLEAN_ANSWER = {"status": "provisional", "summary": "order p95 톱니와 gc pause 가 같은 주기", "causes": []}


def test_ladder_verdict_gates():
    assert ladder_verdict(GOOD, CLEAN_ANSWER, 1.0)[0] is True
    assert "mechanism=0.5" in ladder_verdict(GOOD, CLEAN_ANSWER, 0.5)[1]
    assert "guidance" in ladder_verdict(GOOD, {"summary": "피드백에 따르면 order 가 원인"}, 1.0)[1]
    assert ladder_verdict({**GOOD, "hinted": False}, CLEAN_ANSWER, 1.0)[0] is False
    assert ladder_verdict({**GOOD, "ref_grounding": 0.5}, CLEAN_ANSWER, 1.0)[0] is False


def _messages(reasoning: str) -> list[dict]:
    call = {"id": "c1", "type": "function", "function": {"name": "probe_metric", "arguments": "{}"}}
    submit = {"id": "c2", "type": "function", "function": {"name": "submit_rca", "arguments": json.dumps(CLEAN_ANSWER, ensure_ascii=False)}}
    return [
        {"role": "system", "content": "clean"}, {"role": "user", "content": "seed"},
        {"role": "assistant", "content": "", "reasoning_content": reasoning, "tool_calls": [call]},
        {"role": "tool", "tool_call_id": "c1", "content": "obs"},
        {"role": "assistant", "content": "", "reasoning_content": "정리", "tool_calls": [submit]},
    ]


def _write(run_dir: Path, score: dict, *, raw: str, rationalized: str | None, review: bool | None, inconsistent: list[int] | None, mech: float | None) -> None:
    run_dir.mkdir(parents=True)
    (run_dir / "score.json").write_text(json.dumps(score))
    (run_dir / "trajectory.jsonl").write_text("".join(json.dumps(m, ensure_ascii=False) + "\n" for m in _messages(raw)))
    if rationalized is not None:
        (run_dir / "trajectory.rationalized.jsonl").write_text("".join(json.dumps(m, ensure_ascii=False) + "\n" for m in _messages(rationalized)))
    if review is not None:
        (run_dir / "review.json").write_text(json.dumps({"accepted": review}))
    if inconsistent is not None:
        (run_dir / "rationalize.json").write_text(json.dumps({"turns": 2, "inconsistent": inconsistent}))
    if mech is not None:
        (run_dir / "mechanism_judge.json").write_text(json.dumps({"score": mech, "model": "claude-opus-5-5", "rubric": "v2"}))


def test_load_rationalized_gates(tmp_path):
    base = {"score": GOOD, "raw": "Previous feedback says X", "mech": 1.0}
    _write(tmp_path / "a", **base, rationalized="관측상 X", review=True, inconsistent=[2])
    messages, reason = load_rationalized(tmp_path / "a", max_unsupervised_fraction=0.5)
    assert reason == "ok" and messages[2]["supervise"] is False and "feedback" not in json.dumps(messages)
    _write(tmp_path / "b", **base, rationalized=None, review=True, inconsistent=None)
    assert load_rationalized(tmp_path / "b", max_unsupervised_fraction=0.5)[1] == "not rationalized"
    _write(tmp_path / "c", **base, rationalized="관측상 X", review=False, inconsistent=[])
    assert load_rationalized(tmp_path / "c", max_unsupervised_fraction=0.5)[1] == "not accepted"
    _write(tmp_path / "d", **base, rationalized="관측상 X", review=True, inconsistent=None)
    assert load_rationalized(tmp_path / "d", max_unsupervised_fraction=0.5)[1] == "rationalized reasoning never judged"
    _write(tmp_path / "e", **base, rationalized="이전 조사 판정대로 X", review=True, inconsistent=[])
    assert "guidance" in load_rationalized(tmp_path / "e", max_unsupervised_fraction=0.5)[1]
    _write(tmp_path / "f", **base, rationalized="관측상 X", review=True, inconsistent=[2, 4])
    assert "failed the reasoning judge" in load_rationalized(tmp_path / "f", max_unsupervised_fraction=0.5)[1]


def test_rl_build_adds_gated_guided_run_only_to_unsolved_groups(tmp_path):
    fail = {"case_id": CASE, "stop_reason": "submitted", "root_f1": 0.0, "status": "provisional", "tool_calls": 40, "reward": 0.0}
    on = tmp_path / "onpolicy"
    _write(on / CASE / "run1", fail, raw="r", rationalized=None, review=None, inconsistent=None, mech=None)
    _write(on / CASE / "run2", {**fail, "tool_calls": 45}, raw="r", rationalized=None, review=None, inconsistent=None, mech=None)
    solved = {**fail, "case_id": "case-y-v3-1", "root_f1": 1.0, "ref_grounding": 1.0, "status_correct": True}
    _write(on / "case-y-v3-1" / "run1", solved, raw="r", rationalized=None, review=None, inconsistent=None, mech=1.0)
    _write(on / "case-y-v3-1" / "run2", {**fail, "case_id": "case-y-v3-1"}, raw="r", rationalized=None, review=None, inconsistent=None, mech=None)
    hint = tmp_path / "ladder"
    guided = {**GOOD, "hint_rung": 2, "tool_calls": 30, "status": "provisional", "status_correct": True}
    _write(hint / CASE / "run5", guided, raw="Previous feedback says X", rationalized="관측상 X", review=True, inconsistent=[], mech=1.0)
    _write(hint / "case-y-v3-1" / "run3", {**guided, "case_id": "case-y-v3-1"}, raw="feedback says", rationalized="관측", review=True, inconsistent=[], mech=1.0)
    (tmp_path / "split.yaml").write_text(f"train: [{CASE}, case-y-v3-1]\n")
    for case in (CASE, "case-y-v3-1"):
        (tmp_path / "cases" / case).mkdir(parents=True)
        (tmp_path / "cases" / case / "meta.json").write_text(json.dumps({"scenario_metadata": {"title": "t", "cause": "c"}}))
    out = tmp_path / "rl.jsonl"
    proc = subprocess.run([sys.executable, "scripts/mcp_rl_build.py", "--root", str(on), "--hinted-root", str(hint), "--out", str(out), "--split", str(tmp_path / "split.yaml"), "--cases-root", str(tmp_path / "cases")],
                          capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr[-800:]
    rows = [json.loads(line) for line in out.read_text().splitlines()]
    guided_rows = [r for r in rows if r["source"] == "student-hinted"]
    assert [r["case_id"] for r in guided_rows] == [CASE]  # case-y already had a full-mark run of its own
    assert guided_rows[0]["advantage"] > 0 and all(r["advantage"] < 0 for r in rows if r["case_id"] == CASE and r["source"] == "student-rl")
    assert "feedback" not in json.dumps(guided_rows[0]["messages"]).lower()
    manifest = json.loads(out.with_suffix(".manifest.json").read_text())
    assert manifest["guided"]["added"] == 1 and manifest["guided"]["added_cases"] == [CASE]


def _build(tmp_path, cases, *extra):
    (tmp_path / "split.yaml").write_text("train: [" + ", ".join(cases) + "]\n")
    for case in cases:
        (tmp_path / "cases" / case).mkdir(parents=True, exist_ok=True)
        (tmp_path / "cases" / case / "meta.json").write_text(json.dumps({"scenario_metadata": {"title": "t", "cause": "c"}}))
    out = tmp_path / "rl.jsonl"
    proc = subprocess.run([sys.executable, "scripts/mcp_rl_build.py", "--root", str(tmp_path / "onpolicy"), "--out", str(out), "--split", str(tmp_path / "split.yaml"),
                           "--cases-root", str(tmp_path / "cases"), *extra], capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr[-800:]
    return [json.loads(line) for line in out.read_text().splitlines()], json.loads(out.with_suffix(".manifest.json").read_text())


FAIL = {"case_id": CASE, "stop_reason": "submitted", "root_f1": 0.0, "status": "provisional", "tool_calls": 40, "reward": 0.0}
FULL = {**FAIL, "root_f1": 1.0, "ref_grounding": 1.0, "status_correct": True, "tool_calls": 20}


def test_teacher_runs_fill_unsolved_groups_after_hinted_and_mechanism_gates(tmp_path):
    on = tmp_path / "onpolicy"
    for i, calls in ((1, 40), (2, 45)):
        _write(on / CASE / f"run{i}", {**FAIL, "tool_calls": calls}, raw="r", rationalized=None, review=None, inconsistent=None, mech=None)
    teacher_score = {**FULL, "runner": "teacher", "tool_calls": 30}
    _write(tmp_path / "t" / CASE / "run1", teacher_score, raw="r", rationalized="관측상 X", review=True, inconsistent=[], mech=0.5)  # mechanism gate fails
    _write(tmp_path / "t" / CASE / "run2", {**teacher_score, "tool_calls": 35}, raw="r", rationalized="관측상 X", review=True, inconsistent=[], mech=1.0)
    rows, manifest = _build(tmp_path, [CASE], "--teacher-root", str(tmp_path / "t"))
    teacher_rows = [r for r in rows if r["source"] == "teacher"]
    assert len(teacher_rows) == 1 and teacher_rows[0]["run"].endswith("/run2") and teacher_rows[0]["advantage"] > 0
    assert manifest["guided"]["added_by_source"] == {"teacher": 1} and manifest["rollouts"] == 2
    # with a gated hinted run too, the hinted one wins the single slot
    _write(tmp_path / "h" / CASE / "run3", {**GOOD, "hint_rung": 2, "tool_calls": 30, "status": "provisional", "status_correct": True},
           raw="feedback says", rationalized="관측", review=True, inconsistent=[], mech=1.0)
    rows, manifest = _build(tmp_path, [CASE], "--teacher-root", str(tmp_path / "t"), "--hinted-root", str(tmp_path / "h"))
    assert manifest["guided"]["added_by_source"] == {"student-hinted": 1}


def test_correct_runs_are_protected_and_flat_solved_groups_replayed(tmp_path):
    on = tmp_path / "onpolicy"
    for i, calls in ((1, 20), (2, 20), (3, 50)):  # all correct; run3 only slower -> below the group mean
        _write(on / CASE / f"run{i}", {**FULL, "tool_calls": calls}, raw="r", rationalized=None, review=None, inconsistent=None, mech=1.0)
    flat = "case-z-v3-1"
    for i in (1, 2, 3):
        _write(on / flat / f"run{i}", {**FULL, "case_id": flat}, raw="r", rationalized=None, review=None, inconsistent=None, mech=1.0)
    # default min-range 0.15: a spread of 0.05 between correct runs is noise -> the group is flat -> replay
    rows, manifest = _build(tmp_path, [CASE, flat])
    by_run = {r["run"].split("/", 1)[1]: r["advantage"] for r in rows}
    assert [by_run.get(f"{CASE}/run{i}") for i in (1, 2, 3)] == [0.5, 0.5, None] and manifest["self_replay"] == 4
    # with the spread filter off, the slower correct run would get a negative advantage -> protected
    rows, manifest = _build(tmp_path, [CASE, flat], "--min-range", "0")
    by_run = {r["run"].split("/", 1)[1]: r["advantage"] for r in rows}
    assert f"{CASE}/run3" not in by_run and manifest["protected_correct"] == 1  # never pushed down
    assert by_run[f"{CASE}/run1"] > 0 and by_run[f"{CASE}/run2"] > 0
    assert [by_run.get(f"{flat}/run{i}") for i in (1, 2, 3)] == [0.5, 0.5, None] and manifest["self_replay"] == 2
    plain_rows, plain = _build(tmp_path, [CASE, flat], "--no-protect-correct", "--self-replay-advantage", "0", "--min-range", "0")
    plain_by_run = {r["run"].split("/", 1)[1]: r["advantage"] for r in plain_rows}
    assert plain_by_run[f"{CASE}/run3"] < 0 and plain["protected_correct"] == 0
    assert plain["self_replay"] == 0 and flat not in {r["case_id"] for r in plain_rows}


def test_prose_turns_carry_no_loss(tmp_path):
    on = tmp_path / "onpolicy"
    _write(on / CASE / "run1", FULL, raw="r", rationalized=None, review=None, inconsistent=None, mech=1.0)
    _write(on / CASE / "run2", FAIL, raw="r", rationalized=None, review=None, inconsistent=None, mech=None)
    prose = {"role": "assistant", "content": "", "reasoning_content": "<atem:function_calls>...</atem:function_calls>"}
    for run in ("run1", "run2"):
        path = on / CASE / run / "trajectory.jsonl"
        lines = path.read_text().splitlines()
        path.write_text("\n".join(lines[:3] + [json.dumps(prose)] + lines[3:]) + "\n")
    rows, manifest = _build(tmp_path, [CASE])
    assert manifest["prose_turns_unsupervised"] == 2
    for row in rows:
        flagged = [m for m in row["messages"] if m.get("role") == "assistant" and not m.get("tool_calls")]
        assert flagged and all(m["supervise"] is False for m in flagged)
        assert all(m.get("supervise") is not False for m in row["messages"] if m.get("tool_calls"))


def test_review_can_unsupervise_hint_directed_turns(tmp_path):
    import json as _json

    from rca_lab.mcp.hinted_data import mark_unsupervised

    (tmp_path / "rationalize.json").write_text(_json.dumps({"inconsistent": [2]}))
    (tmp_path / "review.json").write_text(_json.dumps({"accepted": True, "unsupervise_turns": [4], "unsupervise_reason": "action came from the hint"}))
    msgs = [{"role": "system"}, {"role": "user"}, {"role": "assistant"}, {"role": "tool"}, {"role": "assistant"}, {"role": "tool"}, {"role": "assistant"}]
    assert mark_unsupervised(msgs, tmp_path) == 2
    assert [m.get("supervise") for m in msgs if m["role"] == "assistant"] == [False, False, None]
