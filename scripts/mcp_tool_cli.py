"""One-shot CLI over the rca-mcp tools for an in-session teacher agent.

A Claude Code subagent (the blind teacher) cannot speak MCP stdio itself, so it drives the
tools through this script. Every call is appended to a ledger so the episode can be replayed
as a trajectory in exactly the student's message format (assistant tool_call + tool result).

  uv run python scripts/mcp_tool_cli.py --episode <dir> tools
  uv run python scripts/mcp_tool_cli.py --episode <dir> call describe_target '{"target":"<uuid>"}' --thought "왜 이 조회인가"
  uv run python scripts/mcp_tool_cli.py --episode <dir> submit '{"status":"provisional","summary":"…","causes":[…]}' --thought "결론 근거"

<dir> must contain connections.json and seed.json (written by scripts/mcp_case_run.py --prepare).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from rca_lab.mcp.answer import SUBMIT_TOOL, SUBMIT_TOOL_NAME, validate_result
from rca_lab.mcp.client import MCPClient

LEDGER = "ledger.jsonl"


def _episode_paths(episode: Path) -> tuple[dict[str, str], dict[str, Any], Path]:
    connections = json.loads((episode / "connections.json").read_text(encoding="utf-8"))
    seed = json.loads((episode / "seed.json").read_text(encoding="utf-8"))
    return connections, seed, episode / LEDGER


def _client(binary: Path, connections: dict[str, str], seed: dict[str, Any], episode: Path) -> MCPClient:
    window = seed["time_window"]
    command = [str(binary), "-blind", "-first-event", window["first_event"], "-last-event", window["last_event"]]
    return MCPClient(command, {**os.environ, **connections}, log_path=episode / "mcp.stderr")


def _append(ledger: Path, record: dict[str, Any]) -> None:
    with ledger.open("a", encoding="utf-8") as sink:
        sink.write(json.dumps(record, ensure_ascii=False) + "\n")


def _count_calls(ledger: Path) -> int:
    if not ledger.exists():
        return 0
    return sum(1 for line in ledger.read_text(encoding="utf-8").splitlines() if json.loads(line).get("kind") == "call")


def _print_envelope(text: str, limit: int) -> None:
    if len(text) > limit:
        print(text[:limit])
        print(f"\n…[출력 절단: 전체 {len(text)}자 중 {limit}자 — 더 좁은 인자로 다시 조회하라]")
    else:
        print(text)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--episode", required=True, type=Path)
    parser.add_argument("--mcp-binary", type=Path, default=Path(os.environ.get("RCA_MCP_BINARY", ROOT / "tools/rca-mcp/bin/rca-mcp")))
    parser.add_argument("--max-calls", type=int, default=60)
    parser.add_argument("--print-limit", type=int, default=12_000)
    parser.add_argument("--thought", default="", help="why this call is the next step (recorded as the turn's reasoning)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("tools")
    call = sub.add_parser("call")
    call.add_argument("name")
    call.add_argument("arguments")
    submit = sub.add_parser("submit")
    submit.add_argument("arguments")
    args = parser.parse_args()

    connections, seed, ledger = _episode_paths(args.episode)
    if args.command == "tools":
        with _client(args.mcp_binary, connections, seed, args.episode) as client:
            catalog = client.list_tools() + [SUBMIT_TOOL]
        for tool in catalog:
            print(f"## {tool['name']}\n{tool.get('description', '')}\nargs: {json.dumps(tool.get('inputSchema', {}).get('properties', {}), ensure_ascii=False)}\n")
        return 0
    try:
        arguments = json.loads(args.arguments)
    except json.JSONDecodeError as exc:
        print(f"arguments must be a JSON object: {exc}")
        return 2
    if not isinstance(arguments, dict):
        print("arguments must be a JSON object")
        return 2
    if (args.episode / "result.json").exists():
        print("episode already submitted — no further calls are recorded")
        return 3
    if args.command == "submit":
        try:
            result = validate_result(arguments)
        except ValueError as exc:
            _append(ledger, {"kind": "submit_rejected", "ts": time.time(), "thought": args.thought, "arguments": arguments, "error": str(exc)})
            print(str(exc))
            return 1
        (args.episode / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
        _append(ledger, {"kind": "submit", "id": f"call_{uuid.uuid4().hex[:12]}", "ts": time.time(), "thought": args.thought, "name": SUBMIT_TOOL_NAME, "arguments": result})
        print("accepted — 조사 종료")
        return 0
    if _count_calls(ledger) >= args.max_calls:
        print(f"호출 한도 {args.max_calls}회 소진 — submit 으로 결론을 제출하라")
        return 4
    with _client(args.mcp_binary, connections, seed, args.episode) as client:
        text, is_error = client.call(args.name, arguments)
    _append(ledger, {"kind": "call", "id": f"call_{uuid.uuid4().hex[:12]}", "ts": time.time(), "thought": args.thought, "name": args.name, "arguments": arguments, "is_error": is_error, "response": text})
    _print_envelope(text, args.print_limit)
    return 1 if is_error else 0


if __name__ == "__main__":
    sys.exit(main())
