"""Teacher agent: `claude -p` driving the same MCP tools, submitting through `submit_rca`.

The CLI is the only allowed teacher interface (repo rule: no SDK calls). Built-in tools, settings
sources and skills are disabled so the teacher sees nothing but the seed and the tool envelopes.
The working directory carries no case name (blind).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rca_lab.mcp.answer import SUBMIT_TOOL_NAME, validate_result
from rca_lab.mcp.prompts import SYSTEM_PROMPT, raw_system_prompt, user_prompt


@dataclass
class TeacherConfig:
    claude_model: str
    mcp_binary: Path
    max_turns: int = 60
    timeout_seconds: int = 3600
    python_executable: str = sys.executable
    hint: str | None = None  # ladder guidance for this run only; export rebuilds the clean prompt
    tool_surface: str = "harness"  # "raw": harness ablation — generic SQL/PromQL access (rca_lab.mcp.raw_server) instead of rca-mcp


@dataclass
class TeacherRun:
    events: list[dict[str, Any]]
    result: dict[str, Any] | None
    stop_reason: str
    turns: int
    tool_calls: int
    tool_errors: int
    observed_refs: set[str]
    wall_seconds: float
    stderr_tail: str


def _mcp_config(config: TeacherConfig, connections: dict[str, str], window: dict[str, str], result_path: Path) -> dict[str, Any]:
    if config.tool_surface == "raw":
        tools_server = {
            "command": config.python_executable,
            "args": ["-m", "rca_lab.mcp.raw_server"],
            "env": {**{key: connections[key] for key in ("RCA_PG_DSN", "RCA_CH_URL", "RCA_VM_URL") if key in connections},
                    "RCA_MCP_BINARY": str(config.mcp_binary), "PYTHONPATH": str(Path(__file__).resolve().parents[2]), "PATH": os.environ.get("PATH", ""),
                    "RCA_FIRST_EVENT": window["first_event"], "RCA_LAST_EVENT": window["last_event"]},
        }
        answer = _mcp_config(TeacherConfig(**{**config.__dict__, "tool_surface": "harness"}), connections, window, result_path)["mcpServers"]["rca-answer"]
        return {"mcpServers": {"rca-tools": tools_server, "rca-answer": answer}}
    return {
        "mcpServers": {
            "rca-tools": {
                "command": str(config.mcp_binary),
                "args": ["-blind", "-first-event", window["first_event"], "-last-event", window["last_event"]],
                "env": {key: connections[key] for key in ("RCA_PG_DSN", "RCA_CH_URL", "RCA_VM_URL", "RCA_CAPTURE_SOURCES", "RCA_CAPTURE_START", "RCA_NAV_HINTS") if key in connections},
            },
            "rca-answer": {
                "command": config.python_executable,
                "args": ["-m", "rca_lab.mcp.answer_server", str(result_path)],
                "env": {"PYTHONPATH": str(Path(__file__).resolve().parents[2])},
            },
        }
    }


def _collect_refs(payload: Any, sink: set[str]) -> None:
    stack = [payload]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            refs = node.get("refs")
            if isinstance(refs, list):
                sink.update(str(ref) for ref in refs)
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)


def _tool_result_payload(block: dict[str, Any]) -> Any:
    content = block.get("content")
    texts = []
    if isinstance(content, str):
        texts.append(content)
    elif isinstance(content, list):
        texts.extend(str(item.get("text", "")) for item in content if isinstance(item, dict))
    joined = "\n".join(texts)
    try:
        return json.loads(joined)
    except json.JSONDecodeError:
        return joined


def summarize_events(events: list[dict[str, Any]], result_path: Path) -> tuple[dict[str, Any] | None, int, int, int, set[str]]:
    """Return (result, turns, tool_calls, tool_errors, observed_refs) from a stream-json transcript."""
    tool_calls = tool_errors = turns = 0
    observed: set[str] = set()
    result: dict[str, Any] | None = None
    if result_path.exists():
        result = json.loads(result_path.read_text(encoding="utf-8"))
    for event in events:
        if event.get("type") != "assistant" and event.get("type") != "user":
            continue
        for block in (event.get("message") or {}).get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                turns += 1
                name = str(block.get("name", ""))
                if name.endswith(SUBMIT_TOOL_NAME):
                    if result is None:
                        try:
                            result = validate_result(block.get("input") or {})
                        except ValueError:
                            pass
                else:
                    tool_calls += 1
            elif block.get("type") == "tool_result":
                payload = _tool_result_payload(block)
                if block.get("is_error"):
                    tool_errors += 1
                else:
                    _collect_refs(payload, observed)
    return result, turns, tool_calls, tool_errors, observed


def run_teacher(config: TeacherConfig, seed: dict[str, Any], connections: dict[str, str], out_dir: Path) -> TeacherRun:
    out_dir = out_dir.resolve()  # the CLI runs in a blind temp dir; every path it receives must be absolute
    out_dir.mkdir(parents=True, exist_ok=True)
    result_path = out_dir / "result.json"
    if result_path.exists():
        result_path.unlink()
    mcp_path = out_dir / "mcp.json"
    mcp_path.write_text(json.dumps(_mcp_config(config, connections, seed["time_window"], result_path), ensure_ascii=False, indent=1), encoding="utf-8")
    transcript = out_dir / "stream.jsonl"
    started = time.time()
    with tempfile.TemporaryDirectory(prefix="rca-blind-") as workdir:
        command = [
            "claude",
            "-p",
            "--model",
            config.claude_model,
            "--output-format",
            "stream-json",
            "--verbose",
            "--no-session-persistence",
            "--setting-sources",
            "",
            "--strict-mcp-config",
            "--mcp-config",
            str(mcp_path),
            "--tools",
            "",
            "--allowedTools",
            "mcp__rca-tools__*,mcp__rca-answer__*",
            # `--tools ""` alone still let the CLI offer Bash for reading persisted large tool
            # outputs (observed on f01-h); deny every built-in explicitly.
            "--disallowedTools",
            "Bash,Read,Edit,Write,MultiEdit,Glob,Grep,LS,WebFetch,WebSearch,Agent,Task,NotebookEdit,TodoWrite",
            "--max-turns",
            str(config.max_turns),
            "--system-prompt",
            raw_system_prompt() if config.tool_surface == "raw" else SYSTEM_PROMPT,
        ]
        # The prompt goes through stdin so no variadic flag can swallow it.
        env = {key: value for key, value in os.environ.items() if not key.startswith("RCA_")}
        with transcript.open("w", encoding="utf-8") as sink:
            proc = subprocess.run(
                command,
                cwd=workdir,
                env=env,
                input=user_prompt(seed, max_turns=config.max_turns, hint=config.hint),
                stdout=sink,
                stderr=subprocess.PIPE,
                text=True,
                timeout=config.timeout_seconds,
                check=False,
            )
    events = []
    for line in transcript.read_text(encoding="utf-8").splitlines():
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    result, turns, tool_calls, tool_errors, observed = summarize_events(events, result_path)
    if result is not None and not result_path.exists():
        result_path.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    final = next((event for event in reversed(events) if event.get("type") == "result"), {})
    if result is not None:
        stop_reason = "submitted"
    elif proc.returncode != 0:
        stop_reason = f"cli_exit_{proc.returncode}"
    else:
        stop_reason = str(final.get("subtype") or "no_answer")
    return TeacherRun(
        events=events,
        result=result,
        stop_reason=stop_reason,
        turns=turns,
        tool_calls=tool_calls,
        tool_errors=tool_errors,
        observed_refs=observed,
        wall_seconds=time.time() - started,
        stderr_tail=(proc.stderr or "")[-2000:],
    )
