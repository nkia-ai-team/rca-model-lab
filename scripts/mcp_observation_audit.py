"""Deterministic audit for legacy observation slicing versus structured reads.

This audit consumes immutable P0 evidence snapshots only.  It does not grade
model answers and does not inspect holdout/golden data.  Its claim is narrower:
given the tool observations already stored in train-only snapshots, measure
which fields survive the legacy 6000/1200 character views and whether a
structured observation store can make the same stored source reachable again.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

LEGACY_PRESENT_BUDGET = 6000
LEGACY_OLD_BUDGET = 1200
STRUCTURED_PRESENT_BUDGET = 6000
STRUCTURED_READ_BUDGET = 6000
LEGACY_RESPONSE_MARKER = "응답 절단:"
ROLLING_RESPONSE_MARKER = "이전 관측 요약 절단"
STRUCTURED_MODULE = "rca_lab.mcp.observations"
JSON_DECODER = json.JSONDecoder()


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def sha_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[truncated for audit: source_chars={len(text)} visible_chars={limit}]"


def _json_loads(text: str) -> Any | None:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _top_level_value_from_prefix(text: str, key: str) -> tuple[bool, Any]:
    """Return a complete top-level JSON field value from a possibly truncated object prefix."""
    index = 0
    size = len(text)
    while index < size and text[index].isspace():
        index += 1
    if index >= size or text[index] != "{":
        return False, None
    index += 1
    while index < size:
        while index < size and text[index].isspace():
            index += 1
        if index < size and text[index] == "}":
            return False, None
        try:
            parsed_key, index = JSON_DECODER.raw_decode(text, index)
        except json.JSONDecodeError:
            return False, None
        if not isinstance(parsed_key, str):
            return False, None
        while index < size and text[index].isspace():
            index += 1
        if index >= size or text[index] != ":":
            return False, None
        index += 1
        while index < size and text[index].isspace():
            index += 1
        try:
            parsed_value, index = JSON_DECODER.raw_decode(text, index)
        except json.JSONDecodeError:
            return False, None
        if parsed_key == key:
            return True, parsed_value
        while index < size and text[index].isspace():
            index += 1
        if index < size and text[index] == ",":
            index += 1
            continue
        return False, None
    return False, None


def _path_get(value: Any, path: tuple[str, ...]) -> tuple[bool, Any]:
    current = value
    for key in path:
        if not isinstance(current, dict) or key not in current:
            return False, None
        current = current[key]
    return True, current


def _canonical_value(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _same_value_at(source: Any, source_paths: tuple[tuple[str, ...], ...], candidate: Any, candidate_paths: tuple[tuple[str, ...], ...]) -> tuple[bool, bool]:
    source_values = []
    for path in source_paths:
        exists, value = _path_get(source, path)
        if exists:
            source_values.append(_canonical_value(value))
    if not source_values:
        return False, False
    candidate_values = set()
    for path in candidate_paths:
        exists, value = _path_get(candidate, path)
        if exists:
            candidate_values.add(_canonical_value(value))
    return True, any(value in candidate_values for value in source_values)


def _legacy_prefix_matches(source: Any, source_paths: tuple[tuple[str, ...], ...], prefix: str) -> tuple[bool, bool]:
    source_values: list[tuple[str, str]] = []
    for path in source_paths:
        if len(path) != 1:
            continue
        exists, value = _path_get(source, path)
        if exists:
            source_values.append((path[0], _canonical_value(value)))
    if not source_values:
        return False, False
    for key, expected in source_values:
        exists, value = _top_level_value_from_prefix(prefix, key)
        if exists and _canonical_value(value) == expected:
            return True, True
    return True, False


def _load_store_classes() -> tuple[type[Any], Any, str]:
    module = __import__(STRUCTURED_MODULE, fromlist=["ObservationStore", "compact_observation"])
    return module.ObservationStore, module.compact_observation, STRUCTURED_MODULE


def _present(store: Any, text: str, tool_name: str | None, is_error: bool, budget: int) -> str:
    return store.present(text, tool_name=tool_name, is_error=is_error, budget=budget)


def _read(store: Any, args: dict[str, Any], budget: int) -> tuple[str, bool]:
    result = store.read(args, budget=budget)
    if isinstance(result, tuple) and len(result) == 2:
        return str(result[0]), bool(result[1])
    return str(result), False


def _observation_id(view: str) -> str | None:
    loaded = _json_loads(view)
    if not isinstance(loaded, dict):
        return None
    value = loaded.get("observation_id")
    return str(value) if value else None


def _read_full_source(store: Any, obs_id: str, *, budget: int = STRUCTURED_READ_BUDGET, max_pages: int = 100) -> tuple[str, dict[str, Any]]:
    chunks: list[str] = []
    offset = 0
    pages = 0
    seen_offsets: set[int] = set()
    source_sha = None
    source_chars = None
    page_json_chars_max = 0
    page_json_over_budget = 0
    while pages < max_pages:
        if offset in seen_offsets:
            return "".join(chunks), {"ok": False, "reason": "offset_loop", "pages": pages}
        seen_offsets.add(offset)
        text, is_error = _read(store, {"observation_id": obs_id, "offset": offset}, budget)
        pages += 1
        page_json_chars_max = max(page_json_chars_max, len(text))
        page_json_over_budget += int(len(text) > budget)
        page = _json_loads(text)
        if not isinstance(page, dict):
            return "".join(chunks), {"ok": False, "reason": "read_not_json", "pages": pages, "text": text[:300]}
        if is_error and not isinstance(page.get("page", {}).get("text"), str):
            return "".join(chunks), {"ok": False, "reason": "read_error", "pages": pages, "text": text[:300]}
        chunk = page.get("page", {}).get("text")
        if not isinstance(chunk, str):
            return "".join(chunks), {"ok": False, "reason": "missing_chunk", "pages": pages}
        chunks.append(chunk)
        source_sha = page.get("source_sha256", page.get("sha256", source_sha))
        source_chars = page.get("source_chars", page.get("chars", source_chars))
        page_meta = page.get("page") if isinstance(page.get("page"), dict) else {}
        next_offset = page.get("next_offset", page_meta.get("next_offset"))
        if next_offset is None:
            text_joined = "".join(chunks)
            return text_joined, {
                "ok": True,
                "pages": pages,
                "page_json_chars_max": page_json_chars_max,
                "page_json_over_budget": page_json_over_budget,
                "source_sha256": source_sha,
                "source_chars": source_chars,
                "reconstructed_sha256": sha_text(text_joined),
            }
        if not isinstance(next_offset, int) or next_offset <= offset:
            return "".join(chunks), {"ok": False, "reason": "invalid_next_offset", "pages": pages}
        offset = next_offset
    return "".join(chunks), {"ok": False, "reason": "max_pages", "pages": pages}


def _train_cases(split_path: Path) -> tuple[set[str], str]:
    raw = split_path.read_bytes()
    split = yaml.safe_load(raw)
    train = split.get("train") if isinstance(split, dict) else None
    if not isinstance(train, list) or not all(isinstance(case, str) for case in train):
        raise ValueError(f"{split_path} does not contain a string train list")
    return set(train), sha_bytes(raw)


def _load_messages(snapshot_dir: Path, ordinal: str) -> tuple[list[dict[str, Any]], Path, str]:
    path = snapshot_dir / "snapshots" / ordinal / "trajectory.rationalized.jsonl"
    raw = path.read_bytes()
    messages = [json.loads(line) for line in raw.splitlines() if line.strip()]
    return messages, path, sha_bytes(raw)


def _metric_template() -> dict[str, int]:
    return {"available": 0, "legacy_initial": 0, "legacy_old": 0, "structured_present": 0}


def _increment_field_survival(metrics: dict[str, dict[str, int]], field: str, *, source: bool, initial: bool, old: bool, structured: bool) -> None:
    row = metrics.setdefault(field, _metric_template())
    row["available"] += int(source)
    row["legacy_initial"] += int(source and initial)
    row["legacy_old"] += int(source and old)
    row["structured_present"] += int(source and structured)


def _survival(
    metrics: dict[str, dict[str, int]],
    field: str,
    *,
    source_json: Any,
    source_paths: tuple[tuple[str, ...], ...],
    legacy_initial_text: str,
    legacy_old_text: str,
    structured_json: Any,
    structured_paths: tuple[tuple[str, ...], ...],
    structured_old_json: Any,
) -> None:
    source, initial = _legacy_prefix_matches(source_json, source_paths, legacy_initial_text)
    _, old = _legacy_prefix_matches(source_json, source_paths, legacy_old_text)
    _, structured = _same_value_at(source_json, source_paths, structured_json, structured_paths)
    _increment_field_survival(metrics, field, source=source, initial=initial, old=old, structured=structured)
    _, structured_old = _same_value_at(source_json, source_paths, structured_old_json, structured_paths)
    metrics[field]["structured_old"] = metrics[field].get("structured_old", 0) + int(source and structured_old)


def audit_snapshot(snapshot_dir: Path) -> dict[str, Any]:
    manifest_path = snapshot_dir / "manifest.json"
    items_path = snapshot_dir / "items.jsonl"
    split_path = snapshot_dir / "split.yaml"
    manifest_raw = manifest_path.read_bytes()
    items_raw = items_path.read_bytes()
    train, split_sha = _train_cases(split_path)
    Store, compact, store_source = _load_store_classes()
    store = Store(None)
    rows = [json.loads(line) for line in items_raw.splitlines() if line.strip()]
    field_survival: dict[str, dict[str, int]] = {}
    per_ref: list[dict[str, Any]] = []
    unique_source_sha256: set[str] = set()
    stats = {
        "items": len(rows),
        "cases": len({row.get("case_id") for row in rows}),
        "tool_refs": 0,
        "source_sha_mismatches": 0,
        "train_membership_failures": 0,
        "stored_source_already_capped": 0,
        "stored_source_over_legacy_budget": 0,
        "stored_source_rolling_marker": 0,
        "legacy_initial_source_reachable": 0,
        "legacy_old_source_reachable": 0,
        "structured_source_reachable": 0,
        "structured_present_valid_json": 0,
        "structured_old_valid_json": 0,
        "structured_view_over_budget": 0,
        "structured_read_valid_json_pages": 0,
        "structured_read_page_failures": 0,
        "structured_read_page_json_over_budget": 0,
        "structured_read_pages_total": 0,
        "structured_read_pages_max": 0,
        "valid_source_json": 0,
    }
    snapshot_shas: dict[str, str] = {}
    for row in rows:
        case_id = str(row.get("case_id"))
        if case_id not in train:
            stats["train_membership_failures"] += 1
        ordinal = str(row["audit_id"]).split("-", 1)[0]
        messages, trajectory_path, trajectory_sha = _load_messages(snapshot_dir, ordinal)
        snapshot_shas[str(trajectory_path.relative_to(snapshot_dir))] = trajectory_sha
        for ref in row.get("item", {}).get("evidence", {}).get("refs", []):
            if ref.get("role") != "tool":
                continue
            pos = int(ref["message_position"])
            content = str(messages[pos].get("content", ""))
            source_sha_ok = sha_text(content) == ref.get("source_sha256")
            unique_source_sha256.add(str(ref.get("source_sha256")))
            stats["source_sha_mismatches"] += int(not source_sha_ok)
            stats["tool_refs"] += 1
            source_json = _json_loads(content)
            stats["valid_source_json"] += int(source_json is not None)
            legacy_initial = _truncate(content, LEGACY_PRESENT_BUDGET)
            legacy_old = _truncate(content, LEGACY_OLD_BUDGET)
            is_error = source_json == "accepted" or (isinstance(source_json, dict) and "error" in source_json)
            structured_view = _present(store, content, ref.get("tool_name"), is_error, STRUCTURED_PRESENT_BUDGET)
            structured_payload = _json_loads(structured_view)
            structured_old_view = compact(structured_view, LEGACY_OLD_BUDGET)
            structured_old_payload = _json_loads(structured_old_view)
            stats["structured_old_valid_json"] += int(isinstance(structured_old_payload, dict))
            stats["structured_view_over_budget"] += int(len(structured_view) > STRUCTURED_PRESENT_BUDGET or len(structured_old_view) > LEGACY_OLD_BUDGET)
            stats["structured_present_valid_json"] += int(structured_payload is not None)
            obs_id = _observation_id(structured_view)
            reconstructed = ""
            read_meta: dict[str, Any] = {"ok": False, "reason": "missing_observation_id"}
            if obs_id:
                reconstructed, read_meta = _read_full_source(store, obs_id)
            read_ok = bool(read_meta.get("ok")) and reconstructed == content and sha_text(reconstructed) == ref.get("source_sha256")
            stats["structured_source_reachable"] += int(read_ok)
            stats["structured_read_page_failures"] += int(not read_ok)
            stats["structured_read_valid_json_pages"] += int(read_meta.get("pages") or 0) if read_meta.get("ok") else 0
            stats["structured_read_page_json_over_budget"] += int(read_meta.get("page_json_over_budget") or 0)
            stats["structured_read_pages_total"] += int(read_meta.get("pages") or 0)
            stats["structured_read_pages_max"] = max(stats["structured_read_pages_max"], int(read_meta.get("pages") or 0))
            stats["legacy_initial_source_reachable"] += int(legacy_initial == content)
            stats["legacy_old_source_reachable"] += int(legacy_old == content)
            already_capped = LEGACY_RESPONSE_MARKER in content
            stats["stored_source_over_legacy_budget"] += int(len(content) > LEGACY_PRESENT_BUDGET)
            stats["stored_source_already_capped"] += int(already_capped)
            stats["stored_source_rolling_marker"] += int(ROLLING_RESPONSE_MARKER in content)
            structured_json = structured_payload
            _survival(
                field_survival,
                "status",
                source_json=source_json,
                source_paths=(("status",),),
                legacy_initial_text=legacy_initial,
                legacy_old_text=legacy_old,
                structured_json=structured_json,
                structured_old_json=structured_old_payload,
                structured_paths=(("fields", "status"), ("source", "status")),
            )
            _survival(
                field_survival,
                "summary",
                source_json=source_json,
                source_paths=(("summary",),),
                legacy_initial_text=legacy_initial,
                legacy_old_text=legacy_old,
                structured_json=structured_json,
                structured_old_json=structured_old_payload,
                structured_paths=(("fields", "summary"), ("source", "summary")),
            )
            scope_paths = (("scope",), ("scopes",), ("query_scope",), ("window",), ("time_window",))
            _survival(
                field_survival,
                "scope",
                source_json=source_json,
                source_paths=scope_paths,
                legacy_initial_text=legacy_initial,
                legacy_old_text=legacy_old,
                structured_json=structured_json,
                structured_old_json=structured_old_payload,
                structured_paths=tuple(("fields", *path) for path in scope_paths) + tuple(("source", *path) for path in scope_paths),
            )
            per_ref.append(
                {
                    "audit_id": row.get("audit_id"),
                    "case_id": case_id,
                    "message_position": pos,
                    "tool_name": ref.get("tool_name"),
                    "source_chars": len(content),
                    "source_sha256": ref.get("source_sha256"),
                    "source_sha_ok": source_sha_ok,
                    "source_valid_json": source_json is not None,
                    "stored_source_already_capped": already_capped,
                    "legacy_initial_chars": len(legacy_initial),
                    "legacy_old_chars": len(legacy_old),
                    "legacy_initial_exact": legacy_initial == content,
                    "legacy_old_exact": legacy_old == content,
                    "structured_present_chars": len(structured_view),
                    "structured_present_valid_json": structured_payload is not None,
                    "structured_read_exact": read_ok,
                    "structured_read_pages": read_meta.get("pages"),
                    "structured_read_page_json_chars_max": read_meta.get("page_json_chars_max"),
                    "structured_read_page_json_over_budget": read_meta.get("page_json_over_budget"),
                    "structured_read_failure": None if read_ok else read_meta.get("reason"),
                }
            )
    source_lens = [row["source_chars"] for row in per_ref]
    hard_failures = (
        stats["source_sha_mismatches"]
        + stats["train_membership_failures"]
        + stats["structured_read_page_failures"]
        + stats["structured_read_page_json_over_budget"]
        + stats["structured_view_over_budget"]
    )
    report = {
        "status": "complete" if hard_failures == 0 else "failed",
        "claim_scope": "deterministic stored-snapshot observation reachability only; no upstream raw recovery, no LLM grading, and no RCA performance claim",
        "denominator": "referenced tool observations in evidence items; repeated references are counted each time",
        "snapshot_dir": str(snapshot_dir),
        "provenance": {
            "manifest_sha256": sha_bytes(manifest_raw),
            "items_sha256": sha_bytes(items_raw),
            "split_sha256": split_sha,
            "observation_store_source": store_source,
            "observation_store_sha256": sha_bytes((ROOT / "src/rca_lab/mcp/observations.py").read_bytes()),
            "script_sha256": sha_bytes(Path(__file__).read_bytes()),
            "input_manifest": json.loads(manifest_raw),
            "snapshot_trajectory_sha256": snapshot_shas,
        },
        "budgets": {
            "legacy_present_chars": LEGACY_PRESENT_BUDGET,
            "legacy_old_chars": LEGACY_OLD_BUDGET,
            "structured_present_chars": STRUCTURED_PRESENT_BUDGET,
            "structured_read_chars": STRUCTURED_READ_BUDGET,
            "structured_read_page_json_chars_must_be_lte": STRUCTURED_READ_BUDGET,
        },
        "metrics": {
            **stats,
            "unique_source_sha256": len(unique_source_sha256),
            "source_chars_min": min(source_lens) if source_lens else 0,
            "source_chars_max": max(source_lens) if source_lens else 0,
            "source_chars_total": sum(source_lens),
        },
        "field_survival": field_survival,
        "per_ref": per_ref,
        "notes": [
            "stored_source_already_capped means the frozen trajectory source contains the legacy response truncation marker; upstream MCP output beyond that point cannot be reconstructed from this snapshot.",
            "stored_source_over_legacy_budget only means the stored snapshot source exceeds the 6000-character presentation budget; it is not by itself proof that upstream raw was capped.",
            "structured_source_reachable verifies exact reconstruction only of the frozen stored snapshot source, not hidden upstream data that was never stored.",
        ],
    }
    return report


def run(inputs: list[Path], output: Path) -> dict[str, Any]:
    if not inputs:
        raise ValueError("at least one snapshot directory is required")
    output.mkdir(parents=True, exist_ok=True)
    reports = []
    for snapshot_dir in inputs:
        report = audit_snapshot(snapshot_dir)
        name = snapshot_dir.name
        (output / f"{name}.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        reports.append(report)
    aggregate = {
        "status": "complete" if all(report["status"] == "complete" for report in reports) else "failed",
        "claim_scope": "deterministic stored-snapshot observation reachability only; no upstream raw recovery, no LLM grading, and no RCA performance claim",
        "inputs": [str(path) for path in inputs],
        "reports": [f"{path.name}.json" for path in inputs],
        "totals": {},
    }
    numeric_keys = set().union(*(report["metrics"].keys() for report in reports))
    for key in sorted(numeric_keys):
        values = [report["metrics"].get(key) for report in reports]
        if all(isinstance(value, int) for value in values):
            aggregate["totals"][key] = max(values) if key.endswith("_max") else min(values) if key.endswith("_min") else sum(values)
    aggregate["totals"]["unique_source_sha256"] = len({ref["source_sha256"] for report in reports for ref in report["per_ref"]})
    (output / "summary.json").write_text(json.dumps(aggregate, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return aggregate


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, action="append", required=True, help="snapshot directory containing manifest.json, split.yaml, items.jsonl, snapshots/")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = run(args.input, args.output)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result["status"] != "complete":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
