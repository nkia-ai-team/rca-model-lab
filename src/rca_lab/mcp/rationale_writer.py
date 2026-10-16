"""Rationale writer backed by `claude -p`: given the clean rolling context of a turn and the
call that turn makes, write the private reasoning that leads to that call.

Why not the student model: with the target call in the live prompt the student's reasoning is
consistent (9/12 measured) but 11/12 of its texts talk about "the instruction" — the same
failure as with hints ("don't mention it" is not followed). Claude follows that rule, so the
reasoning is consistent and grounded by construction; judge.py still verifies every turn.
The output is a short engineer's scratchpad in the register the student already uses.
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass
from typing import Any, Literal

from rca_lab.mcp.claude_cli import run_claude
from rca_lab.mcp.codex_cli import run_codex

Provider = Literal["claude", "codex"]

WRITER_SYSTEM_PROMPT = """You write the private reasoning of an incident root-cause-analysis agent for one turn of its investigation. You receive the conversation so far (system rules, the alarm seed, the tool calls already made and their results — older results truncated) and the exact tool call the agent makes next.

Write the agent's scratchpad reasoning for this turn: what the observations so far show, what is still unknown, and why this call on this target with these arguments is the right next step. End with the decision to make that call. Requirements:
- Terse, first person plural, English engineer's notes (the register of "We have … Need … Let's …"), 40–140 words, no headings, no lists.
- Cite only facts present in the conversation (handles like T3, metric names, timestamps, log lines). Never invent observations.
- Never mention that the call was given to you, an instruction, a hint, guidance, feedback, a verdict, or a rewrite. The reasoning must read as the agent's own decision.
- Name the tool and target the way the agent would ("describe T6", "grep T8 logs for Exception", "submit").
- For a final submit_rca call: summarize the evidence chain and state the root cause and status the call submits.

Reply with a JSON object only: {"reasoning": "<text>"}."""


@dataclass
class WriterConfig:
    provider: Provider = "claude"
    model: str = "claude-sonnet-5"
    claude_model: str | None = None
    timeout_seconds: int = 600
    retries: int = 2

    def __post_init__(self) -> None:
        if self.claude_model is not None:
            self.model = self.claude_model


def _run_provider(provider: Provider, *, system_prompt: str, prompt: str, model: str, timeout_seconds: int) -> tuple[str | None, str]:
    if provider == "claude":
        return run_claude(system_prompt=system_prompt, prompt=prompt, model=model, timeout_seconds=timeout_seconds)
    if provider == "codex":
        return run_codex(system_prompt=system_prompt, prompt=prompt, model=model, timeout_seconds=timeout_seconds)
    raise ValueError(f"unsupported rationale writer provider: {provider}")


def _transcript(context: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The context as a plain transcript: roles, text, tool calls and results — no reasoning."""
    out = []
    for message in context:
        item: dict[str, Any] = {"role": message.get("role")}
        if message.get("content"):
            item["content"] = message["content"]
        if message.get("tool_calls"):
            item["tool_calls"] = [{"name": call["function"]["name"], "arguments": call["function"].get("arguments")} for call in message["tool_calls"]]
        if message.get("role") == "tool":
            item["name"] = message.get("name")
        out.append(item)
    return out


def _extract_reasoning(text: str) -> str | None:
    """The writer is asked for {"reasoning": "..."} but sometimes answers with the scratchpad text
    itself, or embeds raw newlines inside the JSON string. Accept the JSON (control characters
    allowed), else fall back to the bare text when it carries no JSON at all."""
    bare = text.strip()
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        try:
            reasoning = json.loads(text[start : end + 1], strict=False).get("reasoning")
        except (json.JSONDecodeError, ValueError, TypeError, AttributeError):
            reasoning = None
        if isinstance(reasoning, str) and reasoning.strip():
            return reasoning.strip()
        if bare.startswith("{"):
            return None  # meant to be JSON but unusable
    return bare if bare and len(bare.split()) >= 10 else None


def write_rationale(config: WriterConfig, context: list[dict[str, Any]], name: str, arguments: dict[str, Any]) -> str | None:
    """Return the reasoning text for the turn that calls `name(arguments)` after `context`, or
    None when the writer failed (caller leaves the turn without reasoning)."""
    prompt = json.dumps({"conversation": _transcript(context), "next_call": {"tool": name, "arguments": arguments}}, ensure_ascii=False)
    last = ""
    for attempt in range(config.retries + 1):
        if attempt:
            time.sleep(15 * attempt)  # transient CLI/API failures (parallel shards)
        text, last = _run_provider(config.provider, system_prompt=WRITER_SYSTEM_PROMPT, prompt=prompt, model=config.model, timeout_seconds=config.timeout_seconds)
        if text is None:
            continue
        reasoning = _extract_reasoning(text)
        if reasoning:
            return reasoning
        last = f"no reasoning in result: {text[:160]!r}"
    print(f"[rationale_writer] failed for {name}: {last}", file=sys.stderr, flush=True)
    return None
