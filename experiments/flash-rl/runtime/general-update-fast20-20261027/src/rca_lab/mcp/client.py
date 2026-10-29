"""Minimal MCP stdio client for the rca-mcp server (JSON-RPC 2.0, one message per line)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import IO, Any, Self

PROTOCOL_VERSION = "2024-11-05"


class MCPError(RuntimeError):
    pass


class MCPClient:
    def __init__(self, command: list[str], env: dict[str, str], *, log_path: Path | None = None) -> None:
        self._command = command
        self._env = env
        self._log_path = log_path
        self._proc: subprocess.Popen[str] | None = None
        self._stderr: IO[str] | None = None
        self._next_id = 0

    def __enter__(self) -> Self:
        self.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def start(self) -> None:
        if self._log_path:
            self._stderr = self._log_path.open("a", encoding="utf-8")
        self._proc = subprocess.Popen(
            self._command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self._stderr if self._stderr else subprocess.DEVNULL,
            env=self._env,
            text=True,
            bufsize=1,
        )
        self._request("initialize", {"protocolVersion": PROTOCOL_VERSION, "capabilities": {}, "clientInfo": {"name": "rca-lab", "version": "1"}})

    def close(self) -> None:
        if self._proc is not None:
            if self._proc.stdin:
                self._proc.stdin.close()
            try:
                self._proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._proc.kill()
            self._proc = None
        if self._stderr is not None:
            self._stderr.close()
            self._stderr = None

    def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if self._proc is None or self._proc.stdin is None or self._proc.stdout is None:
            raise MCPError("MCP server is not running")
        self._next_id += 1
        payload = {"jsonrpc": "2.0", "id": self._next_id, "method": method, "params": params}
        self._proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self._proc.stdin.flush()
        while True:
            line = self._proc.stdout.readline()
            if not line:
                code = self._proc.poll()
                raise MCPError(f"MCP server closed the stream (exit={code}) during {method}")
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if message.get("id") == self._next_id:
                return message

    def list_tools(self) -> list[dict[str, Any]]:
        reply = self._request("tools/list", {})
        if "error" in reply:
            raise MCPError(str(reply["error"]))
        return list(reply["result"]["tools"])

    def call(self, name: str, arguments: dict[str, Any]) -> tuple[str, bool]:
        """Return (text, is_error). Protocol errors become error text so the loop can continue."""
        reply = self._request("tools/call", {"name": name, "arguments": arguments})
        if "error" in reply:
            return json.dumps({"error": reply["error"].get("message", str(reply["error"]))}, ensure_ascii=False), True
        result = reply.get("result", {})
        texts = [block.get("text", "") for block in result.get("content", []) if block.get("type") == "text"]
        return "\n".join(texts), bool(result.get("isError"))


def openai_tools(catalog: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool.get("description", ""),
                "parameters": tool.get("inputSchema") or {"type": "object", "properties": {}},
            },
        }
        for tool in catalog
    ]
