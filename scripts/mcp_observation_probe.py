#!/usr/bin/env python3
"""Fixed-state observation-access probe for legacy vs structured observation views.

This is not an RCA evaluation.  It asks a model to copy one exact scalar fact from a
frozen tool observation captured by the evidence audit.  The treatment is only the
observation rendering: legacy receives the old rolling-prefix view; structured-v1
receives an observation envelope plus read_observation access to the frozen text.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from rca_lab.mcp.muse_grammar import guided_request_fields
from rca_lab.mcp.observations import (
    READ_OBSERVATION_TOOL,
    READ_OBSERVATION_TOOL_NAME,
    ObservationStore,
    compact_observation,
)

VERSION = "mcp-observation-probe-v1"
DEFAULT_AUDIT_ROOT = ROOT / "outputs/evidence-audit-20261007"
DEFAULT_OUT = ROOT / "outputs/observation-probe-20261007"
LEGACY_OLD_CHARS = 1200
LEGACY_NOTE = "\n...[legacy old observation truncated; unavailable content must be treated as unseen]"

ANSWER_TOOL_NAME = "answer_probe"
ANSWER_TOOL = {
    "name": ANSWER_TOOL_NAME,
    "description": "Return the exact fact value, or unknown when it is not visible in the supplied observation view.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "value": {"type": "string", "description": "Exact value copied from the observation."},
            "unknown": {"type": "boolean", "description": "True only when the answer is not visible or retrievable."},
        },
        "required": ["value", "unknown"],
        "additionalProperties": False,
    },
}


@dataclass(frozen=True)
class ProbeFact:
    audit_id: str
    case_id: str
    snapshot_path: str
    message_position: int
    tool_name: str
    source_text: str
    source_sha256: str
    source_chars: int
    path: str
    json_pointer: str
    expected: str
    locator_offset: int
    value_offset: int
    tier: str


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def openai_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"type": "function", "function": {"name": tool["name"], "description": tool.get("description", ""), "parameters": tool.get("inputSchema", {"type": "object", "properties": {}})}} for tool in tools]


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def snapshot_index(audit_id: str) -> str:
    return audit_id.split("-", 1)[0]


def load_snapshot_messages(audit_dir: Path, audit_id: str) -> tuple[Path, list[dict[str, Any]]]:
    path = audit_dir / "snapshots" / snapshot_index(audit_id) / "trajectory.rationalized.jsonl"
    if not path.exists():
        raise FileNotFoundError(path)
    return path, load_jsonl(path)


def scalar_leaves(value: Any, prefix: str = "$") -> list[tuple[str, Any]]:
    if isinstance(value, dict):
        leaves: list[tuple[str, Any]] = []
        for key in sorted(value):
            leaves.extend(scalar_leaves(value[key], f"{prefix}.{key}"))
        return leaves
    if isinstance(value, list):
        leaves = []
        for index, item in enumerate(value):
            leaves.extend(scalar_leaves(item, f"{prefix}[{index}]"))
        return leaves
    if value is None or isinstance(value, (str, int, float, bool)):
        return [(prefix, value)]
    return []


def value_offset(source: str, value: Any) -> int:
    needles = []
    if isinstance(value, str):
        needles.append(json.dumps(value, ensure_ascii=False))
        needles.append(value)
    else:
        needles.append(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
        needles.append(str(value).lower() if isinstance(value, bool) else str(value))
    offsets = [source.find(needle) for needle in needles if needle]
    offsets = [offset for offset in offsets if offset >= 0]
    return min(offsets) if offsets else -1


def locator_offset(source: str, path: str, value_at: int) -> int:
    key = path.rsplit(".", 1)[-1]
    if "[" in key:
        key = key.split("[", 1)[0]
    key = key.strip()
    if key:
        marker = json.dumps(key, ensure_ascii=False) + ":"
        found = source.rfind(marker, 0, max(value_at, 0))
        if found >= 0:
            return found
    return max(0, value_at - 80)


def stable_value(value: Any) -> str | None:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        stripped = value.strip()
        if 1 <= len(stripped) <= 140:
            return stripped
    return None


def pointer_to_path(pointer: str) -> str:
    if not pointer.startswith("/"):
        raise ValueError(f"JSON pointer must start with /: {pointer}")
    path = "$"
    for token in pointer.split("/")[1:]:
        token = token.replace("~1", "/").replace("~0", "~")
        path += f"[{token}]" if token.isdigit() else f".{token}"
    return path


def get_pointer_value(payload: Any, pointer: str) -> Any:
    if pointer == "":
        return payload
    if not pointer.startswith("/"):
        raise ValueError(f"JSON pointer must start with /: {pointer}")
    node = payload
    for token in pointer.split("/")[1:]:
        token = token.replace("~1", "/").replace("~0", "~")
        if isinstance(node, list):
            node = node[int(token)]
        elif isinstance(node, dict):
            node = node[token]
        else:
            raise KeyError(pointer)
    return node


def candidate_from_message(audit_dir: Path, item: dict[str, Any], *, min_offset: int, min_chars: int, tier: str) -> ProbeFact | None:
    snapshot_path, messages = load_snapshot_messages(audit_dir, item["audit_id"])
    for message in messages:
        if message.get("role") != "tool":
            continue
        source = str(message.get("content", ""))
        if len(source) < min_chars:
            continue
        try:
            payload = json.loads(source)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict) or "summary" not in payload:
            continue
        for path, raw in scalar_leaves(payload):
            expected = stable_value(raw)
            if expected is None:
                continue
            offset = value_offset(source, raw)
            if offset < min_offset:
                continue
            if expected in source[:LEGACY_OLD_CHARS]:
                continue
            locator = locator_offset(source, path, offset)
            tool_name = str(message.get("name") or "tool")
            return ProbeFact(
                audit_id=item["audit_id"],
                case_id=item["case_id"],
                snapshot_path=str(snapshot_path),
                message_position=int(message.get("message_position") or 0),
                tool_name=tool_name,
                source_text=source,
                source_sha256=sha256(source),
                source_chars=len(source),
                path=path,
                json_pointer="",
                expected=expected,
                locator_offset=locator,
                value_offset=offset,
                tier=tier,
            )
    return None


def select_probe_fact(audit_root: Path) -> ProbeFact:
    for audit_name in ("expanded", "pilot"):
        audit_dir = audit_root / audit_name
        items_path = audit_dir / "items.jsonl"
        if not items_path.exists():
            continue
        items = load_jsonl(items_path)
        for item in items:
            fact = candidate_from_message(audit_dir, item, min_offset=6000, min_chars=6001, tier="source_chars_gt_6000")
            if fact is not None:
                return fact
        for item in items:
            fact = candidate_from_message(audit_dir, item, min_offset=LEGACY_OLD_CHARS, min_chars=LEGACY_OLD_CHARS + 1, tier="old_observation_gt_1200")
            if fact is not None:
                return fact
    raise RuntimeError("no eligible frozen observation fact found")


def load_fixed_fact(path: Path, json_pointer: str) -> ProbeFact:
    previous = json.loads(path.read_text(encoding="utf-8"))
    previous_fact = previous["fact"]
    snapshot_path = Path(previous_fact["snapshot_path"])
    source_text = ""
    for message in load_jsonl(snapshot_path):
        if message.get("role") == "tool" and sha256(str(message.get("content", ""))) == previous_fact["source_sha256"]:
            source_text = str(message.get("content", ""))
            break
    if not source_text:
        raise RuntimeError(f"source observation not found in {snapshot_path}")
    payload = json.loads(source_text)
    raw = get_pointer_value(payload, json_pointer)
    expected = stable_value(raw)
    if expected is None:
        raise RuntimeError(f"fixed pointer {json_pointer} did not resolve to a stable scalar")
    value_at = value_offset(source_text, raw)
    if value_at < 0:
        raise RuntimeError(f"fixed pointer value not found in source text: {json_pointer}")
    return ProbeFact(
        audit_id=str(previous_fact["audit_id"]),
        case_id=str(previous_fact["case_id"]),
        snapshot_path=str(snapshot_path),
        message_position=int(previous_fact["message_position"]),
        tool_name=str(previous_fact["tool_name"]),
        source_text=source_text,
        source_sha256=sha256(source_text),
        source_chars=len(source_text),
        path=pointer_to_path(json_pointer),
        json_pointer=json_pointer,
        expected=expected,
        locator_offset=locator_offset(source_text, pointer_to_path(json_pointer), value_at),
        value_offset=value_at,
        tier=str(previous_fact["tier"]),
    )


def truncated_view(source: str, limit: int) -> str:
    if len(source) <= limit:
        return source
    return source[:limit] + LEGACY_NOTE


def render_structured(source: str, tool_name: str, out_dir: Path, *, initial_budget: int = 6000, compact_budget: int | None = None) -> tuple[str, ObservationStore]:
    store = ObservationStore(out_dir)
    rendered = store.present(source, tool_name=tool_name, is_error=False, budget=initial_budget)
    if compact_budget is not None:
        rendered = compact_observation(rendered, compact_budget) or rendered
    return rendered, store


def answer_matches(answer: dict[str, Any], expected: str) -> bool:
    if bool(answer.get("unknown")):
        return False
    return str(answer.get("value", "")).strip() == expected


def post_chat(endpoint: str, body: dict[str, Any], timeout: int) -> dict[str, Any]:
    request = urllib.request.Request(endpoint.rstrip("/") + "/chat/completions", data=json.dumps(body).encode("utf-8"), headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "ignore")[:1000]
        raise RuntimeError(f"chat completion HTTP {exc.code}: {detail}") from exc


def build_messages(rendered_observation: str, fact: ProbeFact, *, use_pointer: bool) -> list[dict[str, str]]:
    system = (
        "You are running a fixed-state information-access probe, not RCA. "
        "Use tools only. Return answer_probe with the exact scalar value from the observation path. "
        "If the value is not visible or retrievable, return unknown=true and value=\"\". "
        "Do not guess and do not use incident knowledge."
    )
    target = f"Target JSON pointer: {fact.json_pointer}\n" if use_pointer else f"Target JSON path: {fact.path}\n"
    locator = (
        ""
        if use_pointer
        else (
            "Assisted lookup notice: this probe supplies the approximate source character offset so the task measures "
            "retrieval/access, not autonomous evidence discovery.\n"
            f"Start reading near character offset {fact.locator_offset}; the target value begins after offset {fact.value_offset}. "
            "These offsets are locators, not the value.\n"
        )
    )
    user = (
        f"Frozen observation tool: {fact.tool_name}\n"
        f"Frozen observation sha256: {fact.source_sha256}\n"
        f"{target}"
        f"{locator}"
        "Observation view follows:\n"
        "```json\n"
        f"{rendered_observation}\n"
        "```\n"
        "Return only through answer_probe, or use read_observation first if that tool is available."
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def run_condition(
    *,
    endpoint: str,
    model: str,
    condition: str,
    rendered_observation: str,
    fact: ProbeFact,
    store: ObservationStore | None,
    seed: int,
    max_tool_calls: int,
    timeout: int,
    transport: Callable[[str, dict[str, Any], int], dict[str, Any]] = post_chat,
) -> dict[str, Any]:
    tools = [ANSWER_TOOL] if store is None else [READ_OBSERVATION_TOOL, ANSWER_TOOL]
    messages: list[dict[str, Any]] = build_messages(rendered_observation, fact, use_pointer=bool(fact.json_pointer))
    retrieval_calls: list[dict[str, Any]] = []
    usage: list[dict[str, Any]] = []
    exchanges: list[dict[str, Any]] = []
    started = time.time()
    answer: dict[str, Any] | None = None
    stop_reason = "max_tool_calls"
    request_count = 0
    for turn in range(1, max_tool_calls + 1):
        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "tools": openai_tools(tools),
            "tool_choice": "auto",
            "max_tokens": 1024,
            "temperature": 0.0,
            "top_p": 1.0,
            "seed": seed + turn,
            "stream": False,
        }
        body.update(guided_request_fields(openai_tools(tools), reasoning_guard=True))
        request_count += 1
        reply = transport(endpoint, body, timeout)
        exchanges.append({"request": body, "response": reply, "request_sha256": sha256(dumps(body)), "response_sha256": sha256(dumps(reply))})
        usage.append(reply.get("usage", {}))
        choice = reply["choices"][0]
        finish_reason = choice.get("finish_reason")
        message = choice["message"]
        messages.append({"role": "assistant", "content": message.get("content") or "", "tool_calls": message.get("tool_calls") or []})
        calls = message.get("tool_calls") or []
        if not calls:
            stop_reason = "output_budget_exhausted" if finish_reason == "length" else "no_tool_call"
            break
        call = calls[0]
        name = call["function"]["name"]
        raw_args = call["function"].get("arguments") or "{}"
        try:
            args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
        except json.JSONDecodeError as exc:
            messages.append({"role": "tool", "tool_call_id": call.get("id"), "name": name, "content": dumps({"error": f"invalid JSON arguments: {exc}"})})
            continue
        if name == ANSWER_TOOL_NAME:
            answer = {"value": str(args.get("value", "")), "unknown": bool(args.get("unknown"))}
            messages.append({"role": "tool", "tool_call_id": call.get("id"), "name": name, "content": "accepted"})
            stop_reason = "answered"
            break
        if name == READ_OBSERVATION_TOOL_NAME and store is not None:
            text, is_error = store.read(args, budget=6000)
            retrieval_calls.append({"arguments": args, "is_error": is_error, "response_sha256": sha256(text), "response_chars": len(text)})
            messages.append({"role": "tool", "tool_call_id": call.get("id"), "name": name, "content": text})
            continue
        messages.append({"role": "tool", "tool_call_id": call.get("id"), "name": name, "content": dumps({"error": f"unknown tool {name}"})})
    return {
        "condition": condition,
        "model": model,
        "stop_reason": stop_reason,
        "incomplete": stop_reason in {"max_tool_calls", "no_tool_call", "output_budget_exhausted"},
        "answer": answer,
        "fact_match": bool(answer and answer_matches(answer, fact.expected)),
        "retrieval_calls": retrieval_calls,
        "tool_turns": len([m for m in messages if m.get("role") == "assistant" and m.get("tool_calls")]),
        "request_count": request_count,
        "usage": usage,
        "exchanges": exchanges,
        "wall_seconds": round(time.time() - started, 3),
        "messages": messages,
    }


def available_models(endpoint: str, timeout: int) -> set[str]:
    with urllib.request.urlopen(endpoint.rstrip("/") + "/models", timeout=timeout) as response:
        payload = json.load(response)
    return {str(item.get("id")) for item in payload.get("data", [])}


def run_probe(args: argparse.Namespace) -> dict[str, Any]:
    out_dir = args.output
    if args.pointer_v2 and out_dir == DEFAULT_OUT:
        out_dir = DEFAULT_OUT / "pointer-v2"
    out_dir.mkdir(parents=True, exist_ok=True)
    if args.pointer_v2 and args.fixed_fact_result is None:
        args.fixed_fact_result = DEFAULT_OUT / "result.json"
    if args.fixed_fact_result:
        fact = load_fixed_fact(args.fixed_fact_result, args.json_pointer)
    else:
        fact = select_probe_fact(args.audit_root)
    model_ids = available_models(args.endpoint, args.timeout)
    missing = [model for model in args.models if model not in model_ids]
    if missing:
        raise RuntimeError(f"model(s) not served at {args.endpoint}: {missing}")
    source_file = Path(fact.snapshot_path)
    source_file_sha = sha256(source_file.read_text(encoding="utf-8"))
    script_sha = sha256(Path(__file__).read_text(encoding="utf-8"))
    if fact.tier == "source_chars_gt_6000":
        legacy_limit = 6000
        structured_compact_budget = None
    else:
        legacy_limit = LEGACY_OLD_CHARS
        structured_compact_budget = LEGACY_OLD_CHARS
    structured_initial_budget = 6000
    legacy = truncated_view(fact.source_text, legacy_limit)
    structured, store = render_structured(
        fact.source_text,
        fact.tool_name,
        out_dir / "structured-store",
        initial_budget=structured_initial_budget,
        compact_budget=structured_compact_budget,
    )
    frozen_input = {
        "version": VERSION,
        "fact": {
            "audit_id": fact.audit_id,
            "case_id": fact.case_id,
            "tier": fact.tier,
            "snapshot_path": fact.snapshot_path,
            "message_position": fact.message_position,
            "tool_name": fact.tool_name,
            "source_sha256": fact.source_sha256,
            "source_chars": fact.source_chars,
            "path": fact.path,
            "json_pointer": fact.json_pointer,
            "expected": fact.expected,
            "locator_offset": fact.locator_offset,
            "value_offset": fact.value_offset,
            "legacy_limit_chars": legacy_limit,
            "structured_initial_budget": structured_initial_budget,
            "structured_compact_budget": structured_compact_budget,
        },
        "script_sha256": script_sha,
        "legacy_observation": legacy,
        "structured_observation": structured,
        "source_observation": fact.source_text,
    }
    (out_dir / "frozen-input.json").write_text(json.dumps(frozen_input, ensure_ascii=False, indent=1), encoding="utf-8")
    runs: list[dict[str, Any]] = []
    request_budget = args.total_request_budget
    for model in args.models:
        for condition, rendered, condition_store in (
            ("legacy", legacy, None),
            ("structured-v1", structured, store),
        ):
            if request_budget <= 0:
                raise RuntimeError("request budget exhausted before completing planned probe")
            max_calls = min(args.max_tool_calls, request_budget)
            run = run_condition(
                endpoint=args.endpoint,
                model=model,
                condition=condition,
                rendered_observation=rendered,
                fact=fact,
                store=condition_store,
                seed=args.seed,
                max_tool_calls=max_calls,
                timeout=args.timeout,
            )
            request_budget -= run["request_count"]
            runs.append(run)
            (out_dir / f"{model}.{condition}.json").write_text(json.dumps(run, ensure_ascii=False, indent=1), encoding="utf-8")
    result = {
        "version": VERSION,
        "created_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "output_dir": str(out_dir),
        "scope": "fixed-state information access only; not RCA accuracy and not a model-improvement claim",
        "endpoint": args.endpoint,
        "models": args.models,
        "request_limits": {"max_tokens": 1024, "max_tool_calls_per_cell": args.max_tool_calls, "timeout_seconds": args.timeout, "initial_total_request_budget": args.total_request_budget},
        "fact": {
            "audit_id": fact.audit_id,
            "case_id": fact.case_id,
            "tier": fact.tier,
            "snapshot_path": fact.snapshot_path,
            "snapshot_sha256": source_file_sha,
            "message_position": fact.message_position,
            "tool_name": fact.tool_name,
            "source_sha256": fact.source_sha256,
            "source_chars": fact.source_chars,
            "path": fact.path,
            "json_pointer": fact.json_pointer,
            "expected": fact.expected,
            "locator_offset": fact.locator_offset,
            "value_offset": fact.value_offset,
            "legacy_limit_chars": legacy_limit,
            "structured_initial_budget": structured_initial_budget,
            "structured_compact_budget": structured_compact_budget,
            "assisted_lookup": True,
        },
        "rendering": {
            "legacy_sha256": sha256(legacy),
            "legacy_chars": len(legacy),
            "structured_sha256": sha256(structured),
            "structured_chars": len(structured),
        },
        "runs": runs,
        "summary": {
            "cells": len(runs),
            "matches": sum(1 for run in runs if run["fact_match"]),
            "remaining_request_budget": request_budget,
        },
    }
    (out_dir / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    return result


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit-root", type=Path, default=DEFAULT_AUDIT_ROOT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--endpoint", default="http://127.0.0.1:18002/v1")
    parser.add_argument("--models", nargs="+", default=["muse-glimmer-base", "mcp-v2bs-checkpoint-94"])
    parser.add_argument("--seed", type=int, default=7107)
    parser.add_argument("--max-tool-calls", type=int, default=4)
    parser.add_argument("--total-request-budget", type=int, default=12)
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--fixed-fact-result", type=Path)
    parser.add_argument("--json-pointer", default="/findings/1/axis")
    parser.add_argument("--pointer-v2", action="store_true", help="load the first probe fact and use the JSON-pointer v2 output directory by default")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    result = run_probe(args)
    print(json.dumps({"output": str(Path(result["output_dir"]) / "result.json"), "summary": result["summary"], "fact": result["fact"]}, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
