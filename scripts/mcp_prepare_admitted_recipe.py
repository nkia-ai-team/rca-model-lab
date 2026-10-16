"""Build and export an observation-admitted MCP SFT recipe.

This tool does not call models, start services, or train.  It has two phases:

* ``build`` creates immutable evidence-admission candidates from a joint dataset.
* ``export`` requires every candidate to have an exact admission cache entry, then writes an
  approved-only dataset where all non-approved assistant turns are marked ``supervise: false``.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
SCRIPT_DIR = Path(__file__).resolve().parent
for candidate in (SRC, SCRIPT_DIR):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

import mcp_judge_evidence as evidence_judge

from rca_lab.mcp.context import rolling_view
from rca_lab.mcp.evidence_admission import (
    MODEL,
    RUBRIC_VERSION,
    admission,
    cached_verdict,
    item_sha256,
)
from rca_lab.mcp.evidence_audit import build_evidence_item
from rca_lab.train.mcp_sft import _template_messages
from rca_lab.train.recipe import select_turn_indices

SCHEMA = "admitted-recipe-v1"
DEFAULT_MAX_CHARS = 120000
DEFAULT_TURNS_PER_EPISODE = 0  # All eligible turns; no per-episode cap.


class AdmittedRecipeError(RuntimeError):
    """The admitted recipe could not be prepared without weakening evidence contracts."""


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def sha_text(raw: str) -> str:
    return sha_bytes(raw.encode("utf-8"))


def sha_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def rel(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT))
    except ValueError:
        return str(path.resolve())


def local_path(raw: str, *, anchor: Path) -> Path:
    path = Path(raw)
    if path.is_absolute():
        return path
    anchored = (anchor / path).resolve()
    if anchored.exists():
        return anchored
    return (ROOT / path).resolve()


def stable_write(path: Path, content: str) -> None:
    if path.exists():
        old = path.read_text(encoding="utf-8")
        if old != content:
            raise AdmittedRecipeError(f"refusing to overwrite divergent output: {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def stable_json(path: Path, payload: dict[str, Any]) -> None:
    stable_write(path, json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as error:
                raise AdmittedRecipeError(f"invalid JSONL row {path}:{line_no}: {error}") from error
            if not isinstance(value, dict):
                raise AdmittedRecipeError(f"JSONL row must be an object: {path}:{line_no}")
            rows.append(value)
    return rows


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    stable_write(path, "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows))


def row_key(row: dict[str, Any]) -> tuple[str, str]:
    case_id = row.get("case_id")
    run = row.get("run")
    if not isinstance(case_id, str) or not isinstance(run, str):
        raise AdmittedRecipeError("dataset rows require string case_id and run")
    return case_id, run


def reasoning_sha(message: dict[str, Any]) -> str:
    reasoning = message.get("reasoning_content")
    if not isinstance(reasoning, str):
        raise AdmittedRecipeError("assistant target requires string reasoning_content")
    return sha_text(reasoning)


def tool_calls_sha(message: dict[str, Any]) -> str:
    return sha_text(canonical_json(message.get("tool_calls")))


def actor_prefix_sha(messages: list[dict[str, Any]], message_index: int) -> str:
    prepared = _template_messages(messages[:message_index])
    return sha_text(canonical_json(rolling_view(prepared)))


def is_final_submit(message: dict[str, Any]) -> bool:
    return any(
        ((call.get("function") or {}).get("name") == "submit_rca")
        for call in (message.get("tool_calls") or [])
    )


def load_source_map(manifest_path: Path) -> dict[tuple[str, str], dict[str, Any]]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    sources = manifest.get("sources")
    if not isinstance(sources, list) or not sources:
        raise AdmittedRecipeError("joint manifest requires nonempty sources")
    mapping: dict[tuple[str, str], dict[str, Any]] = {}
    for source in sources:
        if not isinstance(source, dict):
            raise AdmittedRecipeError("joint manifest source must be an object")
        key = source.get("key")
        dataset_raw = source.get("dataset")
        expected_sha = source.get("dataset_sha256")
        if not isinstance(key, str) or not isinstance(dataset_raw, str) or not isinstance(expected_sha, str):
            raise AdmittedRecipeError("joint manifest source requires key, dataset, and dataset_sha256")
        dataset = local_path(dataset_raw, anchor=manifest_path.parent)
        actual_sha = sha_file(dataset)
        if actual_sha != expected_sha:
            raise AdmittedRecipeError(f"source dataset SHA mismatch for {key}: {actual_sha} != {expected_sha}")
        for row in read_jsonl(dataset):
            identity = row_key(row)
            previous = mapping.get(identity)
            if previous is not None:
                raise AdmittedRecipeError(f"duplicate source case/run identity: {identity}")
            mapping[identity] = {
                "dataset_key": key,
                "source_dataset": rel(dataset),
                "source_dataset_sha256": actual_sha,
                "source_row": row,
                "source_row_sha256": sha_text(canonical_json(row)),
            }
    return mapping


def attach_dataset_key(row: dict[str, Any], source_map: dict[tuple[str, str], dict[str, Any]]) -> dict[str, Any]:
    identity = row_key(row)
    source = source_map.get(identity)
    if source is None:
        raise AdmittedRecipeError(f"joint row missing from manifest sources: {identity}")
    existing = row.get("dataset_key")
    if existing is not None and existing != source["dataset_key"]:
        raise AdmittedRecipeError(
            f"joint row dataset_key mismatch for {identity}: {existing!r} != {source['dataset_key']!r}"
        )
    out = copy.deepcopy(row)
    out["dataset_key"] = source["dataset_key"]
    return out


def selectable_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    prepared = copy.deepcopy(messages)
    for message in prepared:
        if message.get("role") != "assistant":
            continue
        if not str(message.get("reasoning_content") or "").strip() or not message.get("tool_calls"):
            message["supervise"] = False
    return prepared


def evidence_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    prepared = copy.deepcopy(messages)
    for message in prepared:
        if isinstance(message, dict):
            message.pop("supervise", None)
    return prepared


def candidate_for(
    *,
    row: dict[str, Any],
    row_index: int,
    message_index: int,
    joint_sha256: str,
    max_chars: int,
    source: dict[str, Any],
) -> dict[str, Any]:
    messages = row["messages"]
    message = messages[message_index]
    source_messages = source["source_row"]["messages"]
    item = build_evidence_item(source_messages, message_index, max_chars=max_chars)
    item_hash = item_sha256(item)
    case_id, run = row_key(row)
    return {
        "schema": SCHEMA,
        "audit_id": f"{row_index:05d}-{message_index}",
        "dataset_key": row["dataset_key"],
        "case_id": case_id,
        "run": run,
        "row_index": row_index,
        "message_index": message_index,
        "joint_sha256": joint_sha256,
        "row_sha256": sha_text(canonical_json(row)),
        "source_dataset": source["source_dataset"],
        "source_dataset_sha256": source["source_dataset_sha256"],
        "source_row_sha256": source["source_row_sha256"],
        "reasoning_sha256": reasoning_sha(message),
        "tool_calls_sha256": tool_calls_sha(message),
        "actor_prefix_sha256": actor_prefix_sha(messages, message_index),
        "actor_prefix_policy": "template-normalized arguments + no supervise metadata + rolling_view(recent8,old1200,no prior reasoning)",
        "final_submit": is_final_submit(message),
        "item": item,
        "item_sha256": item_hash,
        "max_chars": max_chars,
        "provider": "codex",
        "model": MODEL,
        "rubric_version": RUBRIC_VERSION,
    }


def build_candidates(
    joint_path: Path,
    manifest_path: Path,
    out_dir: Path,
    *,
    turns_per_episode: int = DEFAULT_TURNS_PER_EPISODE,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> dict[str, Any]:
    if turns_per_episode < 0:
        raise AdmittedRecipeError("--turns-per-episode must be nonnegative (0 = all)")
    if max_chars <= 0:
        raise AdmittedRecipeError("--max-chars must be positive")
    joint_sha = sha_file(joint_path)
    source_map = load_source_map(manifest_path)
    rows = read_jsonl(joint_path)
    if not rows:
        raise AdmittedRecipeError("joint dataset has no rows")
    candidates: list[dict[str, Any]] = []
    source_counts: Counter[str] = Counter()
    case_counts: Counter[str] = Counter()
    final_submit_counts: Counter[str] = Counter()
    seen_identities: set[tuple[str, str]] = set()
    for row_index, raw in enumerate(rows):
        identity = row_key(raw)
        if identity in seen_identities:
            raise AdmittedRecipeError(f"duplicate joint row identity: {identity}")
        seen_identities.add(identity)
        source = source_map.get(identity)
        if source is None:
            raise AdmittedRecipeError(f"joint row missing from manifest sources: {identity}")
        row = attach_dataset_key(raw, source_map)
        messages = row.get("messages")
        if not isinstance(messages, list):
            raise AdmittedRecipeError(f"row messages must be a list: {identity}")
        source_messages = source["source_row"].get("messages")
        if not isinstance(source_messages, list):
            raise AdmittedRecipeError(f"source row messages must be a list: {identity}")
        selected = select_turn_indices(
            selectable_messages(messages),
            turns_per_episode,
            policy="tool_coverage",
            seed=f"{identity[0]}/{identity[1]}",
        )
        for message_index in selected:
            verify_model_visible_source(messages, source_messages, message_index, identity)
            candidate = candidate_for(
                row=row,
                row_index=row_index,
                message_index=message_index,
                joint_sha256=joint_sha,
                max_chars=max_chars,
                source=source,
            )
            candidates.append(candidate)
            source_counts[candidate["dataset_key"]] += 1
            case_counts[candidate["case_id"]] += 1
            if candidate["final_submit"]:
                final_submit_counts[candidate["dataset_key"]] += 1
    if not candidates:
        raise AdmittedRecipeError("no eligible candidate turns selected")

    items_path = out_dir / "items.jsonl"
    blind_path = out_dir / "blind-items.json"
    manifest_out = out_dir / "manifest.json"
    write_jsonl(items_path, candidates)
    stable_write(
        blind_path,
        canonical_json([{"audit_id": row["audit_id"], "item": row["item"], "item_sha256": row["item_sha256"]} for row in candidates]),
    )
    report = {
        "schema": SCHEMA,
        "status": "candidates_prepared_not_judged",
        "joint": rel(joint_path),
        "joint_sha256": joint_sha,
        "joint_manifest": rel(manifest_path),
        "joint_manifest_sha256": sha_file(manifest_path),
        "items": rel(items_path),
        "items_sha256": sha_file(items_path),
        "blind_items": rel(blind_path),
        "blind_items_sha256": sha_file(blind_path),
        "examples": len(rows),
        "cases": len({row_key(row)[0] for row in rows}),
        "candidates": len(candidates),
        "turns_per_episode_cap": turns_per_episode,
        "max_chars": max_chars,
        "provider": "codex",
        "model": MODEL,
        "rubric_version": RUBRIC_VERSION,
        "by_source": dict(sorted(source_counts.items())),
        "by_case": dict(sorted(case_counts.items())),
        "final_submit_candidates_by_source": dict(sorted(final_submit_counts.items())),
        "source_map_identities": len(source_map),
        "selection_policy": "tool_coverage; existing supervise:false remains ineligible; no fill from unassessed turns after admission",
    }
    stable_json(manifest_out, report)
    return report


def load_candidates(path: Path) -> list[dict[str, Any]]:
    rows = read_jsonl(path)
    if not rows:
        raise AdmittedRecipeError("candidate items file is empty")
    return rows


def build_source_lookup(candidates: list[dict[str, Any]]) -> dict[tuple[str, str, str], dict[str, Any]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for candidate in candidates:
        grouped[(str(candidate.get("source_dataset")), str(candidate.get("source_dataset_sha256")))].append(candidate)
    lookup: dict[tuple[str, str, str], dict[str, Any]] = {}
    for (raw_path, expected_sha), group in grouped.items():
        source_path = local_path(raw_path, anchor=ROOT)
        if sha_file(source_path) != expected_sha:
            raise AdmittedRecipeError(f"candidate source dataset hash mismatch: {raw_path}")
        wanted = {(candidate.get("case_id"), candidate.get("run")) for candidate in group}
        found: set[tuple[str, str]] = set()
        for row in read_jsonl(source_path):
            identity = row_key(row)
            if identity not in wanted:
                continue
            key = (raw_path, identity[0], identity[1])
            lookup[key] = row
            found.add(identity)
        missing = sorted(wanted - found)
        if missing:
            raise AdmittedRecipeError(f"candidate source row not found in {raw_path}: {missing[:8]}")
    return lookup


def source_row_for_candidate(
    candidate: dict[str, Any],
    source_lookup: dict[tuple[str, str, str], dict[str, Any]],
) -> dict[str, Any]:
    expected_source_sha = candidate.get("source_dataset_sha256")
    if not isinstance(expected_source_sha, str):
        raise AdmittedRecipeError(f"candidate source dataset hash mismatch: {candidate.get('audit_id')}")
    key = (str(candidate.get("source_dataset")), str(candidate.get("case_id")), str(candidate.get("run")))
    row = source_lookup.get(key)
    if row is None:
        raise AdmittedRecipeError(f"candidate source row not found: {candidate.get('audit_id')}")
    if candidate.get("source_row_sha256") != sha_text(canonical_json(row)):
        raise AdmittedRecipeError(f"candidate source row hash mismatch: {candidate.get('audit_id')}")
    return row


def verify_candidate_binding(
    row: dict[str, Any],
    candidate: dict[str, Any],
    joint_sha256: str,
    *,
    source_lookup: dict[tuple[str, str, str], dict[str, Any]],
) -> None:
    case_id, run = row_key(row)
    message_index = candidate.get("message_index")
    if candidate.get("case_id") != case_id or candidate.get("run") != run or candidate.get("joint_sha256") != joint_sha256:
        raise AdmittedRecipeError(f"candidate source identity mismatch: {candidate.get('audit_id')}")
    if not isinstance(message_index, int):
        raise AdmittedRecipeError(f"candidate message_index must be int: {candidate.get('audit_id')}")
    messages = row.get("messages")
    if not isinstance(messages, list) or message_index < 0 or message_index >= len(messages):
        raise AdmittedRecipeError(f"candidate message_index out of range: {candidate.get('audit_id')}")
    message = messages[message_index]
    if not isinstance(message, dict) or message.get("role") != "assistant":
        raise AdmittedRecipeError(f"candidate target is not assistant: {candidate.get('audit_id')}")
    source_row = source_row_for_candidate(candidate, source_lookup)
    source_messages = source_row.get("messages")
    if not isinstance(source_messages, list):
        raise AdmittedRecipeError(f"candidate source row messages must be a list: {candidate.get('audit_id')}")
    verify_model_visible_source(messages, source_messages, message_index, (case_id, run))
    checks = {
        "reasoning_sha256": reasoning_sha(message),
        "tool_calls_sha256": tool_calls_sha(message),
        "actor_prefix_sha256": actor_prefix_sha(messages, message_index),
        "item_sha256": item_sha256(build_evidence_item(source_messages, message_index, max_chars=int(candidate.get("max_chars") or DEFAULT_MAX_CHARS))),
    }
    for field, actual in checks.items():
        if candidate.get(field) != actual:
            raise AdmittedRecipeError(
                f"{field} mismatch for {case_id}/{run}:{message_index}: "
                f"candidate={candidate.get(field)} actual={actual}"
            )


def verify_model_visible_source(
    joint_messages: list[dict[str, Any]],
    source_messages: list[dict[str, Any]],
    message_index: int,
    identity: tuple[str, str],
) -> None:
    if message_index >= len(source_messages):
        raise AdmittedRecipeError(f"source row missing selected message: {identity}:{message_index}")
    joint_message = joint_messages[message_index]
    source_message = source_messages[message_index]
    checks = {
        "reasoning_sha256": (reasoning_sha(joint_message), reasoning_sha(source_message)),
        "tool_calls_sha256": (tool_calls_sha(joint_message), tool_calls_sha(source_message)),
        "actor_prefix_sha256": (
            actor_prefix_sha(joint_messages, message_index),
            actor_prefix_sha(source_messages, message_index),
        ),
    }
    for field, (joint_value, source_value) in checks.items():
        if joint_value != source_value:
            raise AdmittedRecipeError(
                f"source/quarantine {field} mismatch for {identity[0]}/{identity[1]}:{message_index}"
            )


def resolve_decision(candidate: dict[str, Any], cache_dir: Path) -> dict[str, Any]:
    item = candidate.get("item")
    expected_hash = candidate.get("item_sha256")
    if not isinstance(item, dict) or expected_hash != item_sha256(item):
        raise AdmittedRecipeError(f"candidate item hash mismatch: {candidate.get('audit_id')}")
    # Accept an explicit cache directory as well as the judge output root. The
    # directory contract is structural; verdict provenance is still validated.
    cache_root = cache_dir.parent if cache_dir.name == "cache" else cache_dir
    path = evidence_judge.cache_path(cache_root, item_hash=expected_hash, rubric=RUBRIC_VERSION, model=MODEL)
    if not path.exists():
        raise AdmittedRecipeError(f"pending admission cache for {candidate.get('audit_id')}: {path}")
    record = json.loads(path.read_text(encoding="utf-8"))
    verdict = cached_verdict(item, record)
    if verdict is None:
        raise AdmittedRecipeError(f"invalid or stale admission cache for {candidate.get('audit_id')}: {path}")
    decision = admission(item, verdict)
    return {
        "audit_id": candidate["audit_id"],
        "case_id": candidate["case_id"],
        "run": candidate["run"],
        "dataset_key": candidate["dataset_key"],
        "message_index": candidate["message_index"],
        "item_sha256": expected_hash,
        "decision": decision["decision"],
        "reason": decision["reason"],
        "cache": rel(path),
        "provider": "codex",
        "model": MODEL,
        "rubric_version": RUBRIC_VERSION,
        "final_submit": candidate.get("final_submit") is True,
    }


def candidate_map(candidates: list[dict[str, Any]]) -> dict[tuple[str, str, int], dict[str, Any]]:
    out: dict[tuple[str, str, int], dict[str, Any]] = {}
    for candidate in candidates:
        key = (candidate.get("case_id"), candidate.get("run"), candidate.get("message_index"))
        if not isinstance(key[0], str) or not isinstance(key[1], str) or not isinstance(key[2], int):
            raise AdmittedRecipeError(f"invalid candidate identity: {candidate.get('audit_id')}")
        previous = out.get(key)
        if previous is not None:
            raise AdmittedRecipeError(f"duplicate candidate identity: {key}")
        out[key] = candidate
    return out


def export_admitted(
    joint_path: Path,
    manifest_path: Path,
    candidates_path: Path,
    cache_dir: Path,
    out_dir: Path,
    *,
    turns_per_episode: int = DEFAULT_TURNS_PER_EPISODE,
    minimum_final_per_source: int = 0,
) -> dict[str, Any]:
    joint_sha = sha_file(joint_path)
    source_map = load_source_map(manifest_path)
    raw_rows = read_jsonl(joint_path)
    candidates = load_candidates(candidates_path)
    by_target = candidate_map(candidates)
    source_lookup = build_source_lookup(candidates)
    rows_by_identity: dict[tuple[str, str], dict[str, Any]] = {}
    for raw in raw_rows:
        row = attach_dataset_key(raw, source_map)
        identity = row_key(row)
        if identity in rows_by_identity:
            raise AdmittedRecipeError(f"duplicate joint row identity: {identity}")
        rows_by_identity[identity] = row
    decisions: dict[tuple[str, str, int], dict[str, Any]] = {}
    decision_records: list[dict[str, Any]] = []
    for key, candidate in by_target.items():
        row = rows_by_identity.get((key[0], key[1]))
        if row is None:
            raise AdmittedRecipeError(f"candidate identity missing from joint dataset: {key}")
        verify_candidate_binding(row, candidate, joint_sha, source_lookup=source_lookup)
        record = resolve_decision(candidate, cache_dir)
        decisions[key] = record
        decision_records.append(record)

    unresolved = [key for key in by_target if key not in decisions]
    if unresolved:
        raise AdmittedRecipeError(f"unresolved candidates: {unresolved[:8]}")

    approved = {key for key, record in decisions.items() if record["decision"] == "approve"}
    if not approved:
        raise AdmittedRecipeError("no approved targets; refusing to write an empty admitted training dataset")
    if minimum_final_per_source < 0:
        raise AdmittedRecipeError("--minimum-final-per-source must be nonnegative")
    if minimum_final_per_source:
        final_by_source = Counter(
            record["dataset_key"]
            for record in decision_records
            if record["decision"] == "approve" and record["final_submit"]
        )
        missing = sorted(
            source["dataset_key"]
            for source in source_map.values()
            if final_by_source[source["dataset_key"]] < minimum_final_per_source
        )
        if missing:
            raise AdmittedRecipeError(
                f"approved final submit coverage below {minimum_final_per_source} for sources: {sorted(set(missing))}"
            )

    output_rows: list[dict[str, Any]] = []
    omitted_rows: list[dict[str, Any]] = []
    supervised_all = 0
    supervised_capped = 0
    final_submit_approved = 0
    for identity, original in rows_by_identity.items():
        row = copy.deepcopy(original)
        row_supervised = 0
        for index, message in enumerate(row.get("messages") or []):
            if not isinstance(message, dict) or message.get("role") != "assistant":
                continue
            approved_target = (identity[0], identity[1], index) in approved
            message["supervise"] = approved_target
            if approved_target:
                row_supervised += 1
                supervised_all += 1
                if is_final_submit(message):
                    final_submit_approved += 1
        if row_supervised:
            output_rows.append(row)
            supervised_capped += min(row_supervised, turns_per_episode) if turns_per_episode > 0 else row_supervised
        else:
            omitted_rows.append({"case_id": identity[0], "run": identity[1], "dataset_key": row["dataset_key"]})

    dataset_path = out_dir / "joint.jsonl"
    write_jsonl(dataset_path, output_rows)
    dataset_sha = sha_file(dataset_path)
    decisions_path = out_dir / "admission_decisions.jsonl"
    write_jsonl(decisions_path, decision_records)
    decision_counts = Counter(record["decision"] for record in decision_records)
    by_source: dict[str, Counter[str]] = defaultdict(Counter)
    for record in decision_records:
        by_source[record["dataset_key"]][record["decision"]] += 1
    manifest = {
        "schema": SCHEMA,
        "status": "admitted_approved_only",
        "dataset": rel(dataset_path),
        "dataset_sha256": dataset_sha,
        "examples": len(output_rows),
        "cases": len({row["case_id"] for row in output_rows}),
        "supervised_turns": supervised_all,
        "supervised_turns_capped": supervised_capped,
        "turns_per_episode": turns_per_episode,
        "readiness": {
            "require_judged": True,
            "infrastructure_incomplete": 0,
            "evidence_admission_required": True,
            "all_candidates_resolved": True,
            "approved_only_supervision": True,
            "unassessed_turns_supervise_false": True,
            "complete_marker_written": True,
        },
        "provenance": {
            "source_dataset": rel(joint_path),
            "source_dataset_sha256": joint_sha,
            "source_manifest": rel(manifest_path),
            "source_manifest_sha256": sha_file(manifest_path),
            "candidates": rel(candidates_path),
            "candidates_sha256": sha_file(candidates_path),
            "decisions": rel(decisions_path),
            "decisions_sha256": sha_file(decisions_path),
            "cache_dir": rel(cache_dir),
            "provider": "codex",
            "model": MODEL,
            "rubric_version": RUBRIC_VERSION,
            "hash_policy": "reasoning raw UTF-8; tool_calls/item/actor_prefix canonical JSON; item_sha uses evidence_admission spaced sorted JSON",
        },
        "admission": {
            "candidates": len(decision_records),
            "decisions": dict(sorted(decision_counts.items())),
            "by_source": {source: dict(sorted(counts.items())) for source, counts in sorted(by_source.items())},
            "approved_final_submit": final_submit_approved,
            "omitted_rows_without_approved_targets": omitted_rows,
        },
    }
    manifest_path_out = out_dir / "joint.manifest.json"
    stable_json(manifest_path_out, manifest)
    complete = {
        "sha256": dataset_sha,
        "examples": len(output_rows),
        "cases": manifest["cases"],
        "supervised_turns": supervised_all,
        "turns_per_episode": turns_per_episode,
        "writer_provider": "codex",
        "writer_model": "gpt-6-astra",
        "judge_provider": "codex",
        "judge_model": MODEL,
        "evidence_admission_provider": "codex",
        "evidence_admission_model": MODEL,
        "evidence_admission_rubric": RUBRIC_VERSION,
        "new_judging": False,
        "approved_only": True,
    }
    complete_path = out_dir / "joint.complete.json"
    stable_json(complete_path, complete)
    verify_admitted_dataset(dataset_path, manifest_path_out)
    return {**manifest, "complete_marker": rel(complete_path)}


def verify_admitted_dataset(dataset_path: Path, manifest_path: Path) -> dict[str, Any]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema") != SCHEMA:
        raise AdmittedRecipeError(f"unsupported admitted manifest schema: {manifest.get('schema')!r}")
    if manifest.get("dataset_sha256") != sha_file(dataset_path):
        raise AdmittedRecipeError("admitted dataset hash mismatch")
    readiness = manifest.get("readiness")
    if not isinstance(readiness, dict) or readiness.get("require_judged") is not True:
        raise AdmittedRecipeError("admitted dataset manifest lacks judged readiness")
    provenance = manifest.get("provenance")
    if not isinstance(provenance, dict):
        raise AdmittedRecipeError("admitted manifest lacks provenance")
    decisions_path = local_path(provenance["decisions"], anchor=manifest_path.parent)
    candidates_path = local_path(provenance["candidates"], anchor=manifest_path.parent)
    if provenance.get("decisions_sha256") != sha_file(decisions_path):
        raise AdmittedRecipeError("admission decisions hash mismatch")
    if provenance.get("candidates_sha256") != sha_file(candidates_path):
        raise AdmittedRecipeError("admission candidates hash mismatch")
    decision_rows = read_jsonl(decisions_path)
    candidates = candidate_map(load_candidates(candidates_path))
    source_lookup = build_source_lookup(list(candidates.values()))
    candidate_by_hash = {candidate["item_sha256"]: candidate for candidate in candidates.values()}
    for record in decision_rows:
        if record.get("provider") != "codex" or record.get("model") != MODEL or record.get("rubric_version") != RUBRIC_VERSION:
            raise AdmittedRecipeError(f"decision provenance mismatch: {record.get('audit_id')}")
        candidate = candidate_by_hash.get(record.get("item_sha256"))
        if candidate is None:
            raise AdmittedRecipeError(f"decision has no matching candidate item: {record.get('audit_id')}")
        cache_path = local_path(record["cache"], anchor=manifest_path.parent)
        cache_record = json.loads(cache_path.read_text(encoding="utf-8"))
        verdict = cached_verdict(candidate["item"], cache_record)
        if verdict is None:
            raise AdmittedRecipeError(f"decision cache no longer matches candidate: {record.get('audit_id')}")
        resolved = admission(candidate["item"], verdict)
        if record.get("decision") != resolved["decision"] or record.get("reason") != resolved["reason"]:
            raise AdmittedRecipeError(f"decision record no longer matches cache: {record.get('audit_id')}")
    approved = {
        (record.get("case_id"), record.get("run"), record.get("message_index"))
        for record in decision_rows
        if record.get("decision") == "approve"
    }
    if not approved:
        raise AdmittedRecipeError("admitted manifest has no approved decisions")
    rows = read_jsonl(dataset_path)
    supervised = 0
    seen_approved: set[tuple[str, str, int]] = set()
    for row in rows:
        identity = row_key(row)
        if not isinstance(row.get("dataset_key"), str):
            raise AdmittedRecipeError(f"admitted row missing dataset_key: {identity}")
        for index, message in enumerate(row.get("messages") or []):
            if not isinstance(message, dict) or message.get("role") != "assistant":
                continue
            should_supervise = (identity[0], identity[1], index) in approved
            if message.get("supervise") is not should_supervise:
                raise AdmittedRecipeError(f"supervise flag does not match approval: {identity}:{index}")
            if should_supervise:
                candidate = candidates.get((identity[0], identity[1], index))
                if candidate is None:
                    raise AdmittedRecipeError(f"supervised target has no candidate: {identity}:{index}")
                if candidate.get("dataset_key") != row.get("dataset_key"):
                    raise AdmittedRecipeError(f"supervised target dataset_key mismatch: {identity}:{index}")
                verify_candidate_binding(row, candidate, str(candidate["joint_sha256"]), source_lookup=source_lookup)
                seen_approved.add((identity[0], identity[1], index))
                supervised += 1
    missing_approved = sorted(approved - seen_approved)
    if missing_approved:
        raise AdmittedRecipeError(f"approved decisions missing from admitted dataset: {missing_approved[:8]}")
    if supervised != int(manifest.get("supervised_turns") or -1):
        raise AdmittedRecipeError("supervised turn count mismatch")
    if len(rows) != int(manifest.get("examples") or -1):
        raise AdmittedRecipeError("example count mismatch")
    return {"dataset": str(dataset_path), "examples": len(rows), "supervised_turns": supervised}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build")
    build.add_argument("--joint", type=Path, required=True)
    build.add_argument("--manifest", type=Path, required=True)
    build.add_argument("--out-dir", type=Path, required=True)
    build.add_argument("--turns-per-episode", type=int, default=DEFAULT_TURNS_PER_EPISODE)
    build.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS)
    export = sub.add_parser("export")
    export.add_argument("--joint", type=Path, required=True)
    export.add_argument("--manifest", type=Path, required=True)
    export.add_argument("--candidates", type=Path, required=True)
    export.add_argument("--cache-dir", type=Path, required=True)
    export.add_argument("--out-dir", type=Path, required=True)
    export.add_argument("--turns-per-episode", type=int, default=DEFAULT_TURNS_PER_EPISODE)
    export.add_argument("--minimum-final-per-source", type=int, default=0)
    verify = sub.add_parser("verify")
    verify.add_argument("--dataset", type=Path, required=True)
    verify.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "build":
            report = build_candidates(
                args.joint,
                args.manifest,
                args.out_dir,
                turns_per_episode=args.turns_per_episode,
                max_chars=args.max_chars,
            )
        elif args.command == "export":
            report = export_admitted(
                args.joint,
                args.manifest,
                args.candidates,
                args.cache_dir,
                args.out_dir,
                turns_per_episode=args.turns_per_episode,
                minimum_final_per_source=args.minimum_final_per_source,
            )
        else:
            report = verify_admitted_dataset(args.dataset, args.manifest)
    except AdmittedRecipeError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
