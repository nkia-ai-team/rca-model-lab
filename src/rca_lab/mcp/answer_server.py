"""MCP stdio server exposing only `submit_rca` — lets the claude CLI teacher submit the same
typed answer the student submits. The accepted result is written to the path given as argv[1].

Usage: python -m rca_lab.mcp.answer_server <result.json>
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from rca_lab.mcp.answer import SUBMIT_TOOL, SUBMIT_TOOL_NAME, validate_result


def _reply(identifier: object, result: object) -> None:
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": identifier, "result": result}, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main(result_path: Path) -> None:
    for line in sys.stdin:
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        identifier = message.get("id")
        method = message.get("method")
        if identifier is None:
            continue  # notifications need no reply
        if method == "initialize":
            _reply(identifier, {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}}, "serverInfo": {"name": "rca-answer", "version": "1"}})
        elif method == "tools/list":
            _reply(identifier, {"tools": [SUBMIT_TOOL]})
        elif method == "tools/call":
            params = message.get("params") or {}
            if params.get("name") != SUBMIT_TOOL_NAME:
                _reply(identifier, {"content": [{"type": "text", "text": f"unknown tool {params.get('name')}"}], "isError": True})
                continue
            try:
                result = validate_result(params.get("arguments") or {})
            except ValueError as exc:
                _reply(identifier, {"content": [{"type": "text", "text": str(exc)}], "isError": True})
                continue
            result_path.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
            _reply(identifier, {"content": [{"type": "text", "text": "accepted — 조사를 종료한다. 더 이상 도구를 호출하지 말고 한 줄로 마친다."}]})
        else:
            _reply(identifier, {})


if __name__ == "__main__":
    main(Path(sys.argv[1]))
