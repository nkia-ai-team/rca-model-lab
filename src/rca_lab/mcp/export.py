"""Convert teacher episodes (claude CLI stream-json or the subagent ledger) into the student's
message format so they can be scored, replayed and exported for SFT with one code path.

Output message shape (identical to the student trajectory.jsonl):
  system / user(seed prompt) / assistant{reasoning_content, tool_calls} / tool{content} … /
  assistant{tool_calls:[submit_rca]} / tool("accepted")
When `alias` is set every UUID is replaced by the per-episode T<n> handle exactly as the student
sees it, and the mapping is returned so the result stays scoreable in UUID space.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from rca_lab.mcp.alias import HANDLE_RE, UUIDAliaser
from rca_lab.mcp.answer import SUBMIT_TOOL_NAME, validate_result
from rca_lab.mcp.prompts import SYSTEM_PROMPT, user_prompt
from rca_lab.mcp.student import ALIAS_NOTE, _truncate


def _mcp_tool_name(name: str) -> str:
    """Strip the CLI's MCP prefix: mcp__rca-tools__scan_metrics → scan_metrics."""
    return name.split("__")[-1]


def _turns_from_stream(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return [{thought, name, arguments, response, is_error}] from claude stream-json events."""
    turns: list[dict[str, Any]] = []
    pending: dict[str, dict[str, Any]] = {}
    for event in events:
        message = event.get("message") or {}
        for block in message.get("content") or []:
            if not isinstance(block, dict):
                continue
            if event.get("type") == "assistant" and block.get("type") == "text":
                turns.append({"thought": str(block.get("text", "")), "name": None})
            elif event.get("type") == "assistant" and block.get("type") == "tool_use":
                turn = {"thought": "", "name": _mcp_tool_name(str(block.get("name", ""))), "arguments": block.get("input") or {}, "response": "", "is_error": False}
                if turns and turns[-1]["name"] is None:
                    turn["thought"] = turns.pop()["thought"]
                turns.append(turn)
                pending[str(block.get("id"))] = turn
            elif event.get("type") == "user" and block.get("type") == "tool_result":
                turn = pending.pop(str(block.get("tool_use_id")), None)
                if turn is None:
                    continue
                content = block.get("content")
                if isinstance(content, list):
                    content = "\n".join(str(item.get("text", "")) for item in content if isinstance(item, dict))
                turn["response"] = str(content or "")
                turn["is_error"] = bool(block.get("is_error"))
    return [turn for turn in turns if turn["name"] is not None]


def _turns_from_ledger(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    turns = []
    for record in records:
        if record.get("kind") == "call":
            turns.append(
                {
                    "thought": record.get("thought", ""),
                    "name": record["name"],
                    "arguments": record["arguments"],
                    "response": record.get("response", ""),
                    "is_error": bool(record.get("is_error")),
                }
            )
        elif record.get("kind") == "submit":
            try:
                validate_result(record["arguments"])
            except ValueError as exc:
                turns.append(
                    {
                        "thought": record.get("thought", ""),
                        "name": SUBMIT_TOOL_NAME,
                        "arguments": record["arguments"],
                        "response": json.dumps({"error": str(exc)}, ensure_ascii=False),
                        "is_error": True,
                    }
                )
            else:
                turns.append(
                    {
                        "thought": record.get("thought", ""),
                        "name": SUBMIT_TOOL_NAME,
                        "arguments": record["arguments"],
                        "response": record.get("response", "accepted"),
                        "is_error": bool(record.get("is_error")),
                    }
                )
        elif record.get("kind") == "submit_rejected":
            turns.append(
                {
                    "thought": record.get("thought", ""),
                    "name": SUBMIT_TOOL_NAME,
                    "arguments": record["arguments"],
                    "response": json.dumps({"error": record.get("error", "submit_rca rejected")}, ensure_ascii=False),
                    "is_error": True,
                }
            )
    return turns


def _tool_result_failed(message: dict[str, Any]) -> bool:
    if message.get("is_error") is True:
        return True
    content = message.get("content")
    if not isinstance(content, str):
        return False
    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        return False
    return isinstance(payload, dict) and "error" in payload


def _call_arguments(call: dict[str, Any]) -> dict[str, Any] | None:
    raw = (call.get("function") or {}).get("arguments") or {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw or "{}")
        except json.JSONDecodeError:
            return None
    return raw if isinstance(raw, dict) else None


def _submit_validation_args(args: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return a validation-only copy where issued aliases satisfy validate_result's UUID fields."""
    if args is None:
        return None
    out = json.loads(json.dumps(args, ensure_ascii=False))
    for index, cause in enumerate(out.get("causes") or []):
        if isinstance(cause, dict) and HANDLE_RE.fullmatch(str(cause.get("target", ""))):
            cause["target"] = f"00000000-0000-0000-0000-{index + 1:012d}"
    for index, cause in enumerate(out.get("external_causes") or []):
        if isinstance(cause, dict) and HANDLE_RE.fullmatch(str(cause.get("boundary_target", ""))):
            cause["boundary_target"] = f"10000000-0000-0000-0000-{index + 1:012d}"
    return out


def _submit_schema_error(args: dict[str, Any] | None) -> str | None:
    """Validate submit shape through the shared runtime answer contract."""
    try:
        validate_result(_submit_validation_args(args) or {})
    except ValueError as exc:
        return str(exc)
    return None


def apply_call_supervision(messages: list[dict[str, Any]]) -> int:
    """Keep failed calls as context while excluding their assistant turn from SFT loss.

    A failure is structural metadata (`is_error: true`), a top-level JSON `{"error": ...}` tool
    result, or a submit_rca argument shape that the answer server would reject. Raw strings are
    never substring-matched. Returns the number of newly marked assistant messages.
    """
    tools_by_id = {message.get("tool_call_id"): message for message in messages if message.get("role") == "tool"}
    marked = 0
    for message in messages:
        if message.get("role") != "assistant" or not message.get("tool_calls"):
            continue
        failed = False
        for call in message["tool_calls"]:
            name = (call.get("function") or {}).get("name")
            tool = tools_by_id.get(call.get("id"))
            failed = failed or (tool is not None and _tool_result_failed(tool))
            if name == SUBMIT_TOOL_NAME:
                failed = failed or _submit_schema_error(_call_arguments(call)) is not None
        if failed and message.get("supervise") is not False:
            message["supervise"] = False
            marked += 1
    return marked


def build_messages(seed: dict[str, Any], turns: list[dict[str, Any]], *, max_turns: int, alias: bool, tool_result_max_chars: int) -> tuple[list[dict[str, Any]], dict[str, str]]:
    aliaser = UUIDAliaser() if alias else None

    def show(value: Any) -> Any:
        return aliaser.alias(value) if aliaser else value

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT + (ALIAS_NOTE if aliaser else "")},
        {"role": "user", "content": user_prompt(show(seed), max_turns=max_turns)},
    ]
    for index, turn in enumerate(turns, start=1):
        call_id = f"call_{index:04d}"
        assistant: dict[str, Any] = {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": call_id, "type": "function", "function": {"name": turn["name"], "arguments": json.dumps(show(turn["arguments"]), ensure_ascii=False)}}],
        }
        if turn.get("thought"):
            assistant["reasoning_content"] = show(str(turn["thought"]))
        messages.append(assistant)
        response = str(
            turn.get("response", "")
            or ("accepted" if turn["name"] == SUBMIT_TOOL_NAME and not turn.get("is_error") else "")
        )
        tool = {"role": "tool", "tool_call_id": call_id, "name": turn["name"], "content": _truncate(show(response), tool_result_max_chars)}
        if turn.get("is_error"):
            tool["is_error"] = True
        messages.append(tool)
    apply_call_supervision(messages)
    return messages, (aliaser.mapping if aliaser else {})


def view_source(run_dir: Path) -> str:
    """Which tool responses a teacher run's student-format messages are built from: the replay against the current
    binary (`replay:<sha256-12 of ledger.replay.jsonl>`) or the collection-time record (`collected`). Rationalized
    reasoning is only valid for the view it was written against (mcp_rationalize records it, export checks it)."""
    replay = run_dir / "ledger.replay.jsonl"
    return "replay:" + hashlib.sha256(replay.read_bytes()).hexdigest()[:12] if replay.exists() else "collected"


def load_teacher_turns(run_dir: Path) -> list[dict[str, Any]]:
    """Turns of a teacher run. A replay against the current binary (scripts/mcp_replay_teacher.py) wins over the
    collection-time stream: same calls and reasoning, responses in the format the student sees at evaluation."""
    replay = run_dir / "ledger.replay.jsonl"
    if replay.exists():
        records = [json.loads(line) for line in replay.read_text(encoding="utf-8").splitlines() if line.strip()]
        return _turns_from_ledger(records)
    stream = run_dir / "stream.jsonl"
    ledger = run_dir / "ledger.jsonl"
    if stream.exists():
        events = [json.loads(line) for line in stream.read_text(encoding="utf-8").splitlines() if line.strip()]
        return _turns_from_stream(events)
    if ledger.exists():
        records = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines() if line.strip()]
        return _turns_from_ledger(records)
    raise FileNotFoundError(f"no stream.jsonl or ledger.jsonl in {run_dir}")


def load_run_seed(run_dir: Path, *, cases_root: Path | None = None, mcp_binary: Path | None = None) -> dict[str, Any]:
    """The seed for a run. With `cases_root` + `mcp_binary` the seed is rebuilt from the capture
    with the *current* seed builder and blind filter (seeds are deterministic), so a seed-format
    change applies to every exported row instead of freezing the format at collection time."""
    if cases_root is not None and mcp_binary is not None:
        import subprocess

        from rca_lab.mcp.seed import build_observed_seed

        raw = build_observed_seed(cases_root / run_dir.parent.name)
        proc = subprocess.run([str(mcp_binary), "-sanitize-stdin"], input=json.dumps(raw, ensure_ascii=False), capture_output=True, text=True, check=True)
        return json.loads(proc.stdout)
    path = run_dir.parent / "seed.json" if (run_dir.parent / "seed.json").exists() else run_dir / "seed.json"
    return json.loads(path.read_text(encoding="utf-8"))


def export_teacher_run(run_dir: Path, *, max_turns: int, alias: bool = True, tool_result_max_chars: int = 6_000, cases_root: Path | None = None, mcp_binary: Path | None = None) -> tuple[list[dict[str, Any]], dict[str, str]]:
    seed = load_run_seed(run_dir, cases_root=cases_root, mcp_binary=mcp_binary)
    return build_messages(seed, load_teacher_turns(run_dir), max_turns=max_turns, alias=alias, tool_result_max_chars=tool_result_max_chars)
