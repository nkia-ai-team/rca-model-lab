"""Codex chooses structured actions; the existing RCA loop executes and records them."""
from __future__ import annotations

import hashlib
import json
import logging
import math
import time
from typing import Any

from rca_lab.mcp.codex_cli import run_codex

SYSTEM = """You are the decision component of an RCA tool agent. The caller, not your CLI,
executes the functions declared in the supplied tools catalog and appends their actual responses.
Read the supplied conversation and return the next action as a JSON object, with exactly these keys:
reasoning_content (brief evidence-grounded rationale string), tool_calls (nonempty list of objects
with name string and arguments object). Choose only declared tools and obey tool_choice when given.
Do not run CLI tools or search for MCP resources: the caller executes your returned calls.
Do not invent observations or tool responses. Do not wrap the JSON in Markdown.
"""


def parse_action(text: str, body: dict[str, Any]) -> dict[str, Any]:
    payload = json.loads(text)
    if not isinstance(payload.get("reasoning_content"), str):
        raise TypeError("Codex action has no rationale string")
    calls = payload.get("tool_calls")
    if not isinstance(calls, list) or not calls:
        raise ValueError("Codex action has no tool calls")
    known = {tool["function"]["name"] for tool in body["tools"]}
    request_id = hashlib.sha256(json.dumps(body.get("messages", []), sort_keys=True).encode()).hexdigest()[:16]
    normalized = []
    for index, call in enumerate(calls):
        if call.get("name") not in known or not isinstance(call.get("arguments"), dict):
            raise ValueError("Codex action contains unknown tool or non-object arguments")
        normalized.append({"id": f"codex_{request_id}_{index}", "type": "function", "function": {
            "name": call["name"], "arguments": json.dumps(call["arguments"], ensure_ascii=False)}})
    return {"choices": [{"finish_reason": "tool_calls", "message": {
        "role": "assistant", "content": None, "reasoning_content": payload["reasoning_content"],
        "tool_calls": normalized}}]}


def complete(body: dict[str, Any], *, timeout_seconds: int) -> dict[str, Any]:
    request = {key: body[key] for key in ("messages", "tools", "tool_choice") if key in body}
    # Serialize once: transport retries must not change the action request or replay
    # previously executed RCA tools. Only a successfully parsed action leaves here.
    prompt = json.dumps(request, ensure_ascii=False)
    deadline = time.monotonic() + timeout_seconds
    for attempt in range(3):
        remaining = max(1, math.ceil(deadline - time.monotonic()))
        text, diagnostic = run_codex(system_prompt=SYSTEM, prompt=prompt,
                                     model=body["model"], timeout_seconds=remaining)
        if text is not None:
            return parse_action(text, body)
        # Match the CLI's infrastructure diagnostic, never the model's output.
        # Authentication, quota, malformed actions and generic timeouts are not
        # retried here; a temporary DNS resolver failure is safe to retry.
        dns_retry = diagnostic.startswith("exit ") and ", codex error:" in diagnostic and any(
            marker in diagnostic for marker in (
                "failed to lookup address information: Try again",
                "Temporary failure in name resolution",
            )
        )
        delay = attempt + 1
        if not dns_retry or attempt == 2 or time.monotonic() + delay >= deadline:
            raise RuntimeError(f"Codex completion failed: {diagnostic}")
        logging.getLogger(__name__).warning("Retrying Codex action after temporary DNS failure (%s/2)", attempt + 1)
        time.sleep(delay)
    raise AssertionError("unreachable")
