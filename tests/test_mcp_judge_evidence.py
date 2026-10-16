import importlib.util
import json
from pathlib import Path

import pytest

from rca_lab.mcp import evidence_admission as adm
from rca_lab.mcp.codex_cli import SessionLimitError

SPEC = importlib.util.spec_from_file_location("mcp_judge_evidence", Path(__file__).parents[1] / "scripts/mcp_judge_evidence.py")
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


def _call() -> dict:
    return {"id": "c1", "type": "function", "function": {"name": "describe_target", "arguments": "{}"}}


def _item() -> dict:
    return {
        "target_turn": {"reasoning_content": "describe target", "tool_calls": [_call()]},
        "prior_context": {"omitted_messages": 0},
        "evidence": {
            "refs": [{"ref_id": "m1", "role": "user", "snippet": "alarm on T1"}],
            "omitted": 0,
        },
        "truncation": {"input_truncated": False},
    }


def _verdict() -> dict:
    claim = {
        "status": "supported",
        "evidence_refs": ["m1"],
        "absence_based": False,
        "explanation": "m1 shows the alarm target to inspect",
    }
    return {
        "rationale": claim,
        "action": claim,
        "consistency": {"status": "consistent", "explanation": "both rationale and action inspect T1"},
    }


def _write_items(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


def _row(item: dict | None = None, **extra) -> dict:
    item = item or _item()
    row = {
        "audit_id": "a1",
        "case_id": "secret-case",
        "source": "/secret/source",
        "legacy_verdict": {"grounded": False},
        "item": item,
        "item_sha256": adm.item_sha256(item),
    }
    row.update(extra)
    return row


def test_fresh_call_caches_admission_and_blinds_prompt(tmp_path, monkeypatch):
    input_path = tmp_path / "items.jsonl"
    _write_items(input_path, [_row()])
    calls = []

    def fake_codex(**kwargs):
        calls.append(kwargs)
        return json.dumps(_verdict()), ""

    monkeypatch.setattr(runner, "run_codex", fake_codex)
    report = runner.run(input_path, tmp_path / "out", limit=12, timeout=7)

    assert report["counts"] == {"approved": 1, "held": 0, "rejected": 0, "pending": 0, "total": 1}
    assert calls[0]["model"] == "gpt-6.1-sol"
    assert calls[0]["timeout_seconds"] == 7
    assert calls[0]["prompt"] == json.dumps({"item": _item()}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    assert "legacy_verdict" not in calls[0]["prompt"]
    assert "secret-case" not in calls[0]["prompt"]
    assert "/secret/source" not in calls[0]["prompt"]
    assert json.loads((tmp_path / "out/admission.json").read_text())["items"][0]["decision"] == "approve"


def test_resume_reuses_valid_cache_without_model_call(tmp_path, monkeypatch):
    item = _item()
    input_path = tmp_path / "items.jsonl"
    _write_items(input_path, [_row(item)])
    out = tmp_path / "out"
    cache = runner.cache_path(out, item_hash=adm.item_sha256(item), rubric=adm.RUBRIC_VERSION, model=adm.MODEL)
    runner.atomic_json(cache, adm.verdict_record(item, _verdict()))
    monkeypatch.setattr(runner, "run_codex", lambda **kwargs: pytest.fail("cache should skip rejudge"))

    report = runner.run(input_path, out, limit=12, timeout=5)

    assert report["counts"]["approved"] == 1
    assert report["items"][0]["cached"] is True


def test_changed_hash_or_rubric_invalidates_cache(tmp_path, monkeypatch):
    item = _item()
    input_path = tmp_path / "items.jsonl"
    _write_items(input_path, [_row(item)])
    out = tmp_path / "out"
    old = adm.verdict_record(item, _verdict())
    old["rubric_version"] = "old"
    cache = runner.cache_path(out, item_hash=adm.item_sha256(item), rubric=adm.RUBRIC_VERSION, model=adm.MODEL)
    runner.atomic_json(cache, old)
    calls = []
    monkeypatch.setattr(runner, "run_codex", lambda **kwargs: calls.append(kwargs) or (json.dumps(_verdict()), ""))

    report = runner.run(input_path, out, limit=12, timeout=5)

    assert len(calls) == 1
    assert report["items"][0]["cached"] is False
    assert json.loads(cache.read_text())["rubric_version"] == adm.RUBRIC_VERSION


def test_input_hash_mismatch_is_pending_and_never_calls_model(tmp_path, monkeypatch):
    input_path = tmp_path / "items.jsonl"
    _write_items(input_path, [_row(item_sha256="wrong")])
    monkeypatch.setattr(runner, "run_codex", lambda **kwargs: pytest.fail("bad input hash must not call model"))

    report = runner.run(input_path, tmp_path / "out", limit=12, timeout=5)

    assert report["counts"]["pending"] == 1
    assert report["items"][0]["error_type"] == "input_hash_mismatch"


@pytest.mark.parametrize(
    "reply,error_type",
    [
        ("not json", "malformed_response"),
        ('{"rationale": {}} trailing', "malformed_response"),
        (None, "timeout"),
    ],
)
def test_malformed_or_timeout_stays_pending(tmp_path, monkeypatch, reply, error_type):
    input_path = tmp_path / "items.jsonl"
    _write_items(input_path, [_row()])

    def fake_codex(**kwargs):
        if reply is None:
            return None, "timeout after 5s"
        return reply, ""

    monkeypatch.setattr(runner, "run_codex", fake_codex)
    report = runner.run(input_path, tmp_path / "out", limit=12, timeout=5)

    assert report["counts"]["pending"] == 1
    assert report["items"][0]["error_type"] == error_type
    assert not list((tmp_path / "out").glob("cache/**/*.json"))


def test_quota_stops_without_judging_remaining_items(tmp_path, monkeypatch):
    input_path = tmp_path / "items.jsonl"
    row2 = _row(audit_id="a2")
    _write_items(input_path, [_row(audit_id="a1"), row2])
    calls = []

    def fake_codex(**kwargs):
        calls.append(kwargs)
        raise SessionLimitError("quota")

    monkeypatch.setattr(runner, "run_codex", fake_codex)
    report = runner.run(input_path, tmp_path / "out", limit=12, timeout=5)

    assert len(calls) == 1
    assert report["counts"]["pending"] == 2
    assert report["errors"] == {"not_started": 1, "quota": 1}
    assert [item["audit_id"] for item in report["items"]] == ["a1", "a2"]


def test_ids_file_and_limit_select_rows(tmp_path, monkeypatch):
    input_path = tmp_path / "items.jsonl"
    ids = tmp_path / "ids.txt"
    ids.write_text("a2\n", encoding="utf-8")
    _write_items(input_path, [_row(audit_id="a1"), _row(audit_id="a2")])
    calls = []
    monkeypatch.setattr(runner, "run_codex", lambda **kwargs: calls.append(kwargs) or (json.dumps(_verdict()), ""))

    report = runner.run(input_path, tmp_path / "out", limit=12, timeout=5, ids_file=ids)

    assert len(calls) == 1
    assert report["items"][0]["audit_id"] == "a2"


def test_missing_requested_id_is_pending_not_empty_complete(tmp_path, monkeypatch):
    input_path = tmp_path / "items.jsonl"
    ids = tmp_path / "ids.txt"
    ids.write_text("missing\n", encoding="utf-8")
    _write_items(input_path, [_row(audit_id="a1")])
    monkeypatch.setattr(runner, "run_codex", lambda **kwargs: pytest.fail("missing ids must not call model"))

    report = runner.run(input_path, tmp_path / "out", limit=12, timeout=5, ids_file=ids)

    assert report["status"] == "pending"
    assert report["counts"]["pending"] == 1
    assert report["items"][0]["error_type"] == "missing_requested_id"


def test_malformed_cache_is_quarantined_and_retried(tmp_path, monkeypatch):
    item = _item()
    input_path = tmp_path / "items.jsonl"
    _write_items(input_path, [_row(item)])
    out = tmp_path / "out"
    cache = runner.cache_path(out, item_hash=adm.item_sha256(item), rubric=adm.RUBRIC_VERSION, model=adm.MODEL)
    cache.parent.mkdir(parents=True)
    cache.write_text("{bad", encoding="utf-8")
    calls = []
    monkeypatch.setattr(runner, "run_codex", lambda **kwargs: calls.append(kwargs) or (json.dumps(_verdict()), ""))

    report = runner.run(input_path, out, limit=12, timeout=5)

    assert len(calls) == 1
    assert report["counts"]["approved"] == 1
    assert cache.exists()
    assert list(cache.parent.glob(f"{cache.name}.malformed-*"))


def test_fenced_json_object_is_accepted(tmp_path, monkeypatch):
    input_path = tmp_path / "items.jsonl"
    _write_items(input_path, [_row()])
    monkeypatch.setattr(runner, "run_codex", lambda **kwargs: (f"```json\n{json.dumps(_verdict())}\n```", ""))

    report = runner.run(input_path, tmp_path / "out", limit=12, timeout=5)

    assert report["counts"]["approved"] == 1


def _different_row(index: int) -> dict:
    item = _item()
    item['target_turn']['reasoning_content'] = f'describe target {index}'
    return _row(item, audit_id=f'a{index}')


def test_workers_bound_concurrency_and_preserve_input_order(tmp_path, monkeypatch):
    import threading

    barrier = threading.Barrier(2, timeout=3)
    guard = threading.Lock()
    active = peak = calls = 0

    def fake_codex(**kwargs):
        nonlocal active, peak, calls
        with guard:
            active += 1
            calls += 1
            peak = max(peak, active)
        barrier.wait()
        with guard:
            active -= 1
        return json.dumps(_verdict()), ''

    path = tmp_path / 'items.jsonl'
    _write_items(path, [_different_row(i) for i in range(4)])
    monkeypatch.setattr(runner, 'run_codex', fake_codex)
    report = runner.run(path, tmp_path / 'out', limit=0, timeout=3, workers=2)
    assert calls == 4 and peak == 2
    assert report['counts']['approved'] == 4
    assert [row['audit_id'] for row in report['items']] == ['a0', 'a1', 'a2', 'a3']
    assert json.loads((tmp_path / 'out/admission.json').read_text()) == report


def test_cache_from_validates_exact_provenance_and_preserves_local(tmp_path, monkeypatch):
    item = _item()
    path = tmp_path / 'items.jsonl'
    _write_items(path, [_row(item)])
    wrong_root, valid_root, out = [tmp_path / name for name in ('wrong', 'valid', 'out')]
    bad = adm.verdict_record(item, _verdict())
    bad['model'] = 'different-model'
    key = {'item_hash': adm.item_sha256(item), 'rubric': adm.RUBRIC_VERSION, 'model': adm.MODEL}
    runner.atomic_json(runner.cache_path(wrong_root, **key), bad)
    valid_path = runner.cache_path(valid_root, **key)
    record = adm.verdict_record(item, _verdict())
    runner.atomic_json(valid_path, record)
    original = valid_path.read_bytes()
    monkeypatch.setattr(runner, 'run_codex', lambda **kwargs: pytest.fail('exact external cache must skip model'))

    report = runner.run(path, out, limit=0, timeout=3, workers=2, cache_from=(wrong_root, valid_root))
    assert report['items'][0]['cache_imported_from'] == str(valid_path)
    assert report['items'][0]['cached']
    assert valid_path.read_bytes() == original
    local = runner.cache_path(out, **key).read_bytes()
    # The existing local verdict is immutable even if an external cache changes.
    valid_path.write_text('{bad')
    report = runner.run(path, out, limit=0, timeout=3, cache_from=(valid_root,))
    assert report['items'][0]['cached']
    assert 'cache_imported_from' not in report['items'][0]
    assert runner.cache_path(out, **key).read_bytes() == local


@pytest.mark.parametrize('reply', [json.dumps(_verdict()), 'malformed'])
def test_identical_hash_duplicates_call_once_including_failures(tmp_path, monkeypatch, reply):
    path = tmp_path / 'items.jsonl'
    _write_items(path, [_row(audit_id=f'a{i}') for i in range(5)])
    calls = []
    monkeypatch.setattr(runner, 'run_codex', lambda **kwargs: calls.append(kwargs) or (reply, ''))
    report = runner.run(path, tmp_path / 'out', limit=0, timeout=3, workers=4)
    assert len(calls) == 1
    assert len(report['items']) == 5
    assert all(r['deduplicated_from'] == 'a0' for r in report['items'][1:])
    assert report['counts']['approved' if reply != 'malformed' else 'pending'] == 5


def test_parallel_quota_stops_refill_and_finishes_inflight(tmp_path, monkeypatch):
    import threading

    quota = threading.Event()
    second_started = threading.Event()
    calls = []
    guard = threading.Lock()
    monkeypatch.setattr(runner, 'Event', lambda: quota)

    def fake_codex(**kwargs):
        reasoning = json.loads(kwargs['prompt'])['item']['target_turn']['reasoning_content']
        with guard:
            calls.append(reasoning)
        if reasoning.endswith('0'):
            assert second_started.wait(3)
            raise SessionLimitError('quota')
        second_started.set()
        assert quota.wait(3)
        return json.dumps(_verdict()), ''

    path = tmp_path / 'items.jsonl'
    _write_items(path, [_different_row(i) for i in range(6)])
    monkeypatch.setattr(runner, 'run_codex', fake_codex)
    report = runner.run(path, tmp_path / 'out', limit=0, timeout=3, workers=2)
    assert len(calls) == 2
    assert report['counts'] == {'approved': 1, 'held': 0, 'rejected': 0, 'pending': 5, 'total': 6}
    assert report['errors'] == {'not_started': 4, 'quota': 1}
    assert report['items'][1]['decision'] == 'approve'


def test_transport_exception_is_pending_and_workers_are_bounded(tmp_path, monkeypatch):
    path = tmp_path / 'items.jsonl'
    _write_items(path, [_row()])

    def broken(**kwargs):
        raise OSError('transport disconnected')

    monkeypatch.setattr(runner, 'run_codex', broken)
    report = runner.run(path, tmp_path / 'out', limit=0, timeout=3, workers=2)
    assert report['items'][0]['error_type'] == 'transport_error'
    for count in (0, 17):
        with pytest.raises(ValueError, match='workers'):
            runner.run(path, tmp_path / 'out', limit=0, timeout=3, workers=count)
