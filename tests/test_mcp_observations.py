from __future__ import annotations

import json
from pathlib import Path

import pytest

from rca_lab.mcp.observations import (
    MIN_PRESENTATION_BUDGET,
    ObservationStore,
    compact_observation,
)


def _load(text: str) -> dict:
    return json.loads(text)


def test_present_omits_giant_scalar_without_head_slicing(tmp_path: Path) -> None:
    store = ObservationStore(tmp_path)
    source = {"status": "ok", "summary": "kept", "huge": "A" * 5000, "refs": ["tool:r1"], "truncated": True}

    view = _load(store.present(json.dumps(source), tool_name="sample_logs", is_error=False, budget=1200))

    assert view["version"] == "mcp-observation-v1"
    assert view["tool_name"] == "sample_logs"
    assert view["fields"]["summary"] == "kept"
    assert view["fields"]["refs"] == ["tool:r1"]
    assert "huge" not in view["fields"]
    assert view["omitted_fields"][0]["path"] == "huge"
    assert view["source"]["truncated"] is True
    assert "read_observation" == view["retrieval"]["tool"]
    assert len(json.dumps(view, ensure_ascii=False, sort_keys=True, separators=(",", ":"))) <= 1200


def test_read_pages_reconstruct_unicode_exactly(tmp_path: Path) -> None:
    store = ObservationStore(tmp_path)
    original = json.dumps({"summary": "한글", "rows": ["가나다😀" * 1000]}, ensure_ascii=False)
    view = _load(store.present(original, tool_name="probe", is_error=False, budget=1400))
    observation_id = view["observation_id"]
    chunks: list[str] = []
    offset = 0

    while True:
        page, is_error = store.read({"observation_id": observation_id, "offset": offset, "limit": 777}, budget=1600)
        assert not is_error
        payload = _load(page)
        assert len(page) <= 1600
        chunks.append(payload["page"]["text"])
        if payload["page"]["complete"]:
            break
        offset = payload["page"]["next_offset"]

    assert "".join(chunks) == original


def test_compact_retains_retrieval_metadata_and_facts(tmp_path: Path) -> None:
    store = ObservationStore(tmp_path)
    view = store.present(json.dumps({"summary": "fact", "refs": ["r"], "rows": list(range(1000))}), "probe", False, 2000)

    compact = _load(compact_observation(view, budget=800) or "")

    assert compact["observation_id"] == _load(view)["observation_id"]
    assert compact["fields"]["summary"] == "fact"
    assert compact["fields"]["refs"] == ["r"]
    assert compact["retrieval"]["tool"] == "read_observation"
    assert len(json.dumps(compact, ensure_ascii=False, sort_keys=True, separators=(",", ":"))) <= 800
    assert compact_observation("legacy plain text", budget=800) is None


def test_critical_source_metadata_survives_min_budget(tmp_path: Path) -> None:
    store = ObservationStore(tmp_path)
    source = {
        "status": "no_data",
        "summary": "S" * 4000,
        "truncated": True,
        "no_data": True,
        "no_data_reason": "source table unavailable",
        "rows": ["x" * 1000],
    }

    view = _load(store.present(json.dumps(source), tool_name="sample_logs", is_error=False, budget=800))
    source_meta = view["source"]

    assert source_meta["status"] == "no_data"
    assert source_meta["truncated"] is True
    assert source_meta["no_data"] is True
    assert source_meta["no_data_reason"] == "source table unavailable"
    assert view["presentation"]["source_truncated"] is True


def test_unknown_ids_and_invalid_offsets_fail_closed(tmp_path: Path) -> None:
    store = ObservationStore(tmp_path)
    store.present("known", "probe", False, 800)

    unknown, is_error = store.read({"observation_id": "../" + "x" * 5000}, budget=800)
    assert is_error
    payload = _load(unknown)
    assert payload["error"] == "unknown observation_id"
    assert ".." in payload["observation_id"]
    assert len(unknown) <= 800

    observation_id = _load(store.present("abc", "probe", False, 800))["observation_id"]
    for args in (
        {"observation_id": observation_id, "offset": -1},
        {"observation_id": observation_id, "offset": True},
        {"observation_id": observation_id, "offset": 99},
    ):
        _, failed = store.read(args, budget=800)
        assert failed


def test_deterministic_ids_and_budget_floor(tmp_path: Path) -> None:
    a = ObservationStore(tmp_path / "a")
    b = ObservationStore(tmp_path / "b")
    first = _load(a.present('{"summary":"same"}', "probe", False, 800))
    second = _load(b.present('{"summary":"same"}', "probe", False, 800))
    different_tool = _load(b.present('{"summary":"same"}', "other", False, 800))

    assert first["observation_id"] == second["observation_id"]
    assert first["sha256"] == second["sha256"]
    assert first["observation_id"] != different_tool["observation_id"]
    with pytest.raises(ValueError, match=str(MIN_PRESENTATION_BUDGET)):
        a.present("x", "probe", False, 799)
    with pytest.raises(ValueError, match=str(MIN_PRESENTATION_BUDGET)):
        a.read({"observation_id": first["observation_id"]}, 799)


def test_pointer_selects_exact_repeated_field_and_compact_retries_same_page():
    store = ObservationStore()
    source = json.dumps({"findings": [{"axis": "first"}, {"axis": "second"}], "a/b": {"~key": [42]}})
    view = _load(store.present(source, "probe", False, 1200))
    for pointer, expected in [("/findings/1/axis", "second"), ("/a~1b/~0key/0", 42)]:
        text, error = store.read({"observation_id": view["observation_id"], "json_pointer": pointer}, 1200)
        assert not error
        page = _load(text)["page"]
        assert json.loads(page["text"]) == expected and page["complete"]
        compact = _load(compact_observation(text, 800))
        args = compact["retrieval"]["arguments"]
        assert args["offset"] == 0 and args["json_pointer"] == pointer
        reread, error = store.read(args, 1200)
        assert not error and _load(reread)["page"]["text"] == page["text"]
    for pointer in ["/absent", "/findings/01", "/findings/-1", "/findings/2", "/a~2b", "findings", None]:
        _, error = store.read({"observation_id": view["observation_id"], "json_pointer": pointer}, 1200)
        assert error


def test_large_summary_does_not_prevent_raw_retrieval():
    store = ObservationStore()
    source = json.dumps({"status": "no_data", "no_data_reason": "not_collected", "summary": "x" * 9000, "truncated": True})
    view = _load(store.present(source, "probe", False, 800))
    text, error = store.read({"observation_id": view["observation_id"], "offset": 0, "limit": 100}, 1200)
    assert not error
    page = _load(text)
    assert page["page"]["text"] == source[:100]
    assert page["source"]["truncated"] is True
    assert page["source"]["no_data_reason"] == "not_collected"


def test_page_budget_counts_json_escaping_and_rejects_cross_run_ids():
    store = ObservationStore()
    source = '\"\\\n' * 2000
    view = _load(store.present(source, "probe", False, 800))
    text, error = store.read({"observation_id": view["observation_id"], "limit": 12000}, 800)
    assert not error and len(text) <= 800
    page = _load(text)["page"]
    assert page["text"] == source[:page["returned_chars"]]
    assert page["returned_chars"] > 0
    assert ObservationStore().read({"observation_id": view["observation_id"]}, 800)[1]
