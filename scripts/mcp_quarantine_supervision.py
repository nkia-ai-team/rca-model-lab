"""Create a quarantine-masked joint SFT candidate without approving new data.

The input quarantine file contains exact turn identities from an evidence audit.
This script fails closed unless every reject/hold item can be matched back to the
original joint dataset by row identity, turn index, reasoning hash, tool-call hash,
and rollout-context actor-prefix hash. The only dataset mutation is adding or
preserving ``"supervise": false`` on matching assistant turns.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from rca_lab.mcp.context import rolling_view
from rca_lab.train.mcp_sft import _template_messages

DEFAULT_JOINT = ROOT / "outputs/joint-recovered-20261008/datasets/joint.jsonl"
DEFAULT_QUARANTINE = ROOT / "outputs/recipe-audit-20261008/evidence-quarantine.json"
DEFAULT_OUTPUT = ROOT / "outputs/recipe-audit-20261008/quarantined"
SCHEMA = "quarantine-supervision-candidate-v1"
SUPPORTED_QUARANTINE_SCHEMA = "exact-observation-admission-quarantine-v1"
DEFAULT_TURNS_PER_EPISODE = 6


class QuarantineError(RuntimeError):
    """Quarantine masking failed without producing a candidate dataset."""


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


def stable_write(path: Path, content: str) -> None:
    if path.exists():
        old = path.read_text(encoding="utf-8")
        if old != content:
            raise QuarantineError(f"refusing to overwrite divergent output: {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def load_quarantine(path: Path, joint_sha256: str) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise QuarantineError(f"invalid quarantine JSON: {path}: {error}") from error
    if data.get("schema") != SUPPORTED_QUARANTINE_SCHEMA:
        raise QuarantineError(f"unsupported quarantine schema: {data.get('schema')!r}")
    if data.get("joint_sha256") != joint_sha256:
        raise QuarantineError(
            f"joint SHA mismatch: quarantine={data.get('joint_sha256')} actual={joint_sha256}"
        )
    items = data.get("items")
    if not isinstance(items, list):
        raise QuarantineError("quarantine items must be a list")
    return data


def row_key(row: dict[str, Any]) -> tuple[str, str]:
    case_id = row.get("case_id")
    run = row.get("run")
    if not isinstance(case_id, str) or not isinstance(run, str):
        raise QuarantineError("joint rows require string case_id and run")
    return case_id, run


def decision_key(item: dict[str, Any]) -> tuple[str, str, int]:
    case_id = item.get("case_id")
    run = item.get("run")
    message_index = item.get("message_index")
    if not isinstance(case_id, str) or not isinstance(run, str) or not isinstance(message_index, int):
        raise QuarantineError(f"invalid quarantine identity: {item!r}")
    return case_id, run, message_index


def actor_prefix_sha(messages: list[dict[str, Any]], message_index: int) -> str:
    prepared = _template_messages(messages[:message_index])
    prefix = rolling_view(prepared)
    return sha_text(canonical_json(prefix))


def tool_calls_sha(message: dict[str, Any]) -> str:
    return sha_text(canonical_json(message.get("tool_calls")))


def reasoning_sha(message: dict[str, Any]) -> str:
    reasoning = message.get("reasoning_content")
    if not isinstance(reasoning, str):
        raise QuarantineError("assistant turn is missing string reasoning_content")
    return sha_text(reasoning)


def verify_item(row: dict[str, Any], item: dict[str, Any]) -> None:
    case_id, run, message_index = decision_key(item)
    if (case_id, run) != row_key(row):
        raise QuarantineError(f"row identity mismatch for {case_id}/{run}:{message_index}")
    messages = row.get("messages")
    if not isinstance(messages, list):
        raise QuarantineError(f"row has no messages list: {case_id}/{run}")
    if message_index < 0 or message_index >= len(messages):
        raise QuarantineError(f"message_index out of range: {case_id}/{run}:{message_index}")
    message = messages[message_index]
    if not isinstance(message, dict) or message.get("role") != "assistant":
        raise QuarantineError(f"quarantine target is not an assistant turn: {case_id}/{run}:{message_index}")
    expected = {
        "reasoning_sha256": reasoning_sha(message),
        "tool_calls_sha256": tool_calls_sha(message),
        "actor_prefix_sha256": actor_prefix_sha(messages, message_index),
    }
    for field, actual in expected.items():
        if item.get(field) != actual:
            raise QuarantineError(
                f"{field} mismatch for {case_id}/{run}:{message_index}: "
                f"quarantine={item.get(field)} actual={actual}"
            )


def build_decision_map(quarantine: dict[str, Any]) -> dict[tuple[str, str, int], dict[str, Any]]:
    decisions: dict[tuple[str, str, int], dict[str, Any]] = {}
    for raw in quarantine["items"]:
        if not isinstance(raw, dict):
            raise QuarantineError("quarantine item must be an object")
        decision = raw.get("decision")
        if decision not in {"reject", "hold"}:
            raise QuarantineError(f"unsupported quarantine decision: {decision!r}")
        if raw.get("exact_source_and_actor_prefix_verified") is not True:
            raise QuarantineError(f"unverified quarantine item: {raw.get('audit_id')}")
        key = decision_key(raw)
        previous = decisions.get(key)
        if previous is not None and previous != raw:
            raise QuarantineError(f"conflicting duplicate quarantine item: {key}")
        decisions[key] = raw
    return decisions


def mask_joint(joint_path: Path, quarantine_path: Path, out_dir: Path) -> dict[str, Any]:
    joint_sha = sha_file(joint_path)
    quarantine = load_quarantine(quarantine_path, joint_sha)
    decisions = build_decision_map(quarantine)
    rows_seen: set[tuple[str, str]] = set()
    applied: dict[tuple[str, str, int], str] = {}
    output_lines: list[str] = []
    original_supervised_all = 0
    candidate_supervised_all = 0
    original_supervised_capped = 0
    candidate_supervised_capped = 0
    examples = 0
    cases: set[str] = set()
    already_false = 0
    changed_false = 0

    with joint_path.open(encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                original = json.loads(line)
            except json.JSONDecodeError as error:
                raise QuarantineError(f"invalid joint JSONL row {line_no}: {error}") from error
            key = row_key(original)
            if key in rows_seen:
                raise QuarantineError(f"duplicate joint row case/run identity: {key}")
            rows_seen.add(key)
            cases.add(key[0])
            row = copy.deepcopy(original)
            messages = row.get("messages")
            if not isinstance(messages, list):
                raise QuarantineError(f"joint row messages must be a list: {key}")
            row_original_supervised = 0
            row_candidate_supervised = 0
            for index, message in enumerate(messages):
                if isinstance(message, dict) and message.get("role") == "assistant":
                    if message.get("supervise") is not False:
                        original_supervised_all += 1
                        row_original_supervised += 1
                    decision = decisions.get((key[0], key[1], index))
                    if decision is not None:
                        verify_item(original, decision)
                        if message.get("supervise") is False:
                            already_false += 1
                        else:
                            changed_false += 1
                        message["supervise"] = False
                        applied[(key[0], key[1], index)] = decision["decision"]
                    if message.get("supervise") is not False:
                        candidate_supervised_all += 1
                        row_candidate_supervised += 1
            original_supervised_capped += min(row_original_supervised, DEFAULT_TURNS_PER_EPISODE)
            candidate_supervised_capped += min(row_candidate_supervised, DEFAULT_TURNS_PER_EPISODE)
            examples += 1
            output_lines.append(json.dumps(row, ensure_ascii=False) + "\n")

    missing = sorted(set(decisions) - set(applied))
    if missing:
        raise QuarantineError(f"quarantine identities not found in joint dataset: {missing[:8]}")

    dataset_path = out_dir / "joint.quarantined.jsonl"
    dataset_content = "".join(output_lines)
    stable_write(dataset_path, dataset_content)
    dataset_sha = sha_file(dataset_path)
    summary = {
        "schema": SCHEMA,
        "status": "candidate_only_not_readiness_or_admission_approval",
        "dataset": rel(dataset_path),
        "dataset_sha256": dataset_sha,
        "source_dataset": rel(joint_path),
        "source_dataset_sha256": joint_sha,
        "quarantine": rel(quarantine_path),
        "quarantine_sha256": sha_file(quarantine_path),
        "quarantine_schema": quarantine["schema"],
        "hash_policy": (
            "reasoning raw UTF-8 sha256; tool_calls and actor_prefix canonical JSON "
            "ensure_ascii=False sort_keys=True compact; actor_prefix uses "
            "_template_messages(messages[:index]) then rolling_view"
        ),
        "examples": examples,
        "cases": len(cases),
        "quarantine_items": len(decisions),
        "decisions": {
            "reject": sum(1 for decision in applied.values() if decision == "reject"),
            "hold": sum(1 for decision in applied.values() if decision == "hold"),
        },
        "already_unsupervised": already_false,
        "newly_unsupervised": changed_false,
        "turns_per_episode_cap": DEFAULT_TURNS_PER_EPISODE,
        "original_supervised_turns_capped": original_supervised_capped,
        "candidate_supervised_turns_capped": candidate_supervised_capped,
        "original_supervised_assistant_turns_all_messages": original_supervised_all,
        "candidate_supervised_assistant_turns_all_messages": candidate_supervised_all,
        "unassessed_policy": quarantine.get(
            "unknown_policy", "unchanged; not approved by absence from quarantine"
        ),
        "readiness": {
            "approved_for_training": False,
            "complete_marker_written": False,
            "admission_approval": False,
            "unassessed_turns_remain_unassessed": True,
        },
    }
    manifest_path = out_dir / "manifest.json"
    stable_write(manifest_path, json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--joint", type=Path, default=DEFAULT_JOINT)
    parser.add_argument("--quarantine", type=Path, default=DEFAULT_QUARANTINE)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    try:
        summary = mask_joint(args.joint, args.quarantine, args.out_dir)
    except QuarantineError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
