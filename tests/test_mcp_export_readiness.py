import json
import subprocess
import sys
from pathlib import Path

CASE = "case-train-v3-1"


def _call(index: int, name: str, arguments: dict | None = None) -> dict:
    return {"id": f"c{index}", "type": "function", "function": {"name": name, "arguments": json.dumps(arguments or {})}}


def _messages(*, invalid_submit: bool = False) -> list[dict]:
    submit_args = {"status": "provisional", "summary": "s", "causes": "[]", "external_causes": []} if invalid_submit else {"status": "insufficient", "summary": "s", "causes": [], "external_causes": []}
    return [
        {"role": "system", "content": "clean"},
        {"role": "user", "content": "seed"},
        {"role": "assistant", "content": "", "reasoning_content": "inspect target", "tool_calls": [_call(1, "submit_rca", submit_args)]},
        {"role": "tool", "tool_call_id": "c1", "name": "submit_rca", "content": "accepted"},
    ]


def _write_run(root: Path, *, rationalize: dict | None, case_id: str = CASE, status_correct: bool = True, invalid_submit: bool = False) -> Path:
    run = root / case_id / "run1"
    run.mkdir(parents=True)
    score = {"case_id": case_id, "stop_reason": "submitted", "root_f1": 1.0, "tool_calls": 1, "status_correct": status_correct, "ref_grounding": 1.0}
    (run / "score.json").write_text(json.dumps(score), encoding="utf-8")
    (run / "trajectory.rationalized.jsonl").write_text("".join(json.dumps(message) + "\n" for message in _messages(invalid_submit=invalid_submit)), encoding="utf-8")
    if rationalize is not None:
        (run / "rationalize.json").write_text(json.dumps(rationalize), encoding="utf-8")
    return run


def _run_export(
    tmp_path: Path,
    teacher_root: Path,
    train_cases: list[str] | None = None,
    *,
    require_judged: bool = True,
    validation_cases: list[str] | None = None,
    unsupervise_wrong_status: bool = False,
) -> subprocess.CompletedProcess[str]:
    split = tmp_path / "split.yaml"
    split.write_text("train: [" + ", ".join(train_cases or [CASE]) + "]\nvalidation: [" + ", ".join(validation_cases or []) + "]\nsealed_eval: []\n", encoding="utf-8")
    catalog = tmp_path / "tools.json"
    catalog.write_text("[]", encoding="utf-8")
    cmd = [
        sys.executable,
        "scripts/mcp_export_sft.py",
        "--teacher-root",
        str(teacher_root),
        "--split",
        str(split),
        "--catalog",
        str(catalog),
        "--out",
        str(tmp_path / "sft.jsonl"),
        "--require-rationalized",
        "--max-unsupervised-fraction",
        "1.0",
    ]
    if require_judged:
        cmd.append("--require-judged")
    if unsupervise_wrong_status:
        cmd.append("--unsupervise-wrong-status")
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=False,
    )


def test_export_fails_closed_when_train_rationalized_run_was_never_judged(tmp_path: Path) -> None:
    teacher = tmp_path / "teacher"
    _write_run(teacher, rationalize=None)

    proc = _run_export(tmp_path, teacher)

    assert proc.returncode != 0
    assert "refusing to export: 1 train run(s) have incomplete rationalization/judging" in proc.stderr
    assert "missing rationalize.json" in proc.stderr
    assert not (tmp_path / "sft.jsonl").exists()
    assert not (tmp_path / "sft.manifest.json").exists()


def test_export_allows_judged_negative_turns_and_counts_them_separately(tmp_path: Path) -> None:
    teacher = tmp_path / "teacher"
    _write_run(teacher, rationalize={"turns": 1, "failed": [], "judge": {"2": {"id": 2, "consistent": False, "grounded": True, "issue": "wrong next step"}}, "inconsistent": [2]})

    proc = _run_export(tmp_path, teacher)

    assert proc.returncode == 0, proc.stderr
    rows = [json.loads(line) for line in (tmp_path / "sft.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1
    assert rows[0]["unsupervised_turns"] == 1
    assert rows[0]["messages"][2]["supervise"] is False
    manifest = json.loads((tmp_path / "sft.manifest.json").read_text(encoding="utf-8"))
    assert manifest["unsupervised_turns"] == 1
    assert manifest["readiness"] == {"require_judged": True, "infrastructure_incomplete": 0}


def test_export_fails_closed_on_cached_judge_errors_even_with_inconsistent_list(tmp_path: Path) -> None:
    teacher = tmp_path / "teacher"
    _write_run(
        teacher,
        rationalize={
            "turns": 1,
            "failed": [],
            "judge": {"2": {"id": 2, "error": "You've hit your weekly limit · resets Oct 10, 3pm (UTC)"}},
            "inconsistent": [2],
        },
    )

    proc = _run_export(tmp_path, teacher)

    assert proc.returncode != 0
    assert "refusing to export: 1 train run(s) have incomplete rationalization/judging" in proc.stderr
    assert "judge error for turn 2" in proc.stderr
    assert not (tmp_path / "sft.jsonl").exists()
    assert not (tmp_path / "sft.manifest.json").exists()


def test_export_readiness_gate_ignores_non_train_candidates(tmp_path: Path) -> None:
    teacher = tmp_path / "teacher"
    _write_run(teacher, rationalize=None, case_id="case-heldout-v3-1")

    proc = _run_export(tmp_path, teacher, train_cases=[CASE])

    assert proc.returncode == 0, proc.stderr
    manifest = json.loads((tmp_path / "sft.manifest.json").read_text(encoding="utf-8"))
    assert manifest["examples"] == 0


def test_require_judged_is_opt_in_for_legacy_exports(tmp_path: Path) -> None:
    teacher = tmp_path / "teacher"
    _write_run(teacher, rationalize=None)

    proc = _run_export(tmp_path, teacher, require_judged=False)

    assert proc.returncode == 0, proc.stderr
    assert (tmp_path / "sft.jsonl").exists()


def test_validation_case_is_refused_before_train_filter(tmp_path: Path) -> None:
    teacher = tmp_path / "teacher"
    _write_run(
        teacher,
        rationalize={"turns": 1, "failed": [], "judge": {"2": {"id": 2, "consistent": True, "grounded": True, "issue": ""}}, "inconsistent": []},
        case_id="case-validation-v3-1",
    )

    proc = _run_export(tmp_path, teacher, train_cases=[CASE], validation_cases=["case-validation-v3-1"])

    assert proc.returncode != 0
    assert "refusing validation/sealed cases in training data: ['case-validation-v3-1']" in proc.stderr
    assert not (tmp_path / "sft.jsonl").exists()


def test_manifest_unsupervised_counts_use_final_masks_without_mislabeling_status(tmp_path: Path) -> None:
    teacher = tmp_path / "teacher"
    verdict = {"turns": 1, "failed": [], "judge": {"2": {"id": 2, "consistent": True, "grounded": True, "issue": ""}}, "inconsistent": []}
    _write_run(teacher, rationalize=verdict, invalid_submit=True)

    proc = _run_export(tmp_path, teacher)

    assert proc.returncode == 0, proc.stderr
    manifest = json.loads((tmp_path / "sft.manifest.json").read_text(encoding="utf-8"))
    assert manifest["unsupervised_turns"] == 1
    assert manifest["call_supervision_errors"] == 1
    assert manifest["status_mismatch_episodes"] == 0


def test_status_mismatch_manifest_counts_only_status_mask(tmp_path: Path) -> None:
    teacher = tmp_path / "teacher"
    verdict = {"turns": 1, "failed": [], "judge": {"2": {"id": 2, "consistent": True, "grounded": True, "issue": ""}}, "inconsistent": []}
    _write_run(teacher, rationalize=verdict, status_correct=False)

    proc = _run_export(tmp_path, teacher, unsupervise_wrong_status=True)

    assert proc.returncode == 0, proc.stderr
    manifest = json.loads((tmp_path / "sft.manifest.json").read_text(encoding="utf-8"))
    assert manifest["unsupervised_turns"] == 1
    assert manifest["call_supervision_errors"] == 0
    assert manifest["status_mismatch_episodes"] == 1
