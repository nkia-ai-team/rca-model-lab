"""LLM judge for rationalized reasoning: does a turn's reasoning justify the action that follows?

Rationalization (rationalize.py) regenerates the student's private reasoning while the tool call
of every turn is fixed. Nothing there checks *content*: a reasoning that ends with "let's describe
T1" in front of a `sample_logs` call on T8 passes, and training on such turns teaches
"explanation and action are unrelated". This module asks a judge model, per turn, whether the
reasoning (a) states a next step that matches the actual call (tool intent and target/parameters)
and (b) draws only on observations (no guidance, feedback or prior-verdict talk). The judge is
`claude -p` (CLI subprocess, per repo rule) with a JSON-only contract; the verdicts are used to
resample turns and to exclude stubborn ones from supervision — they never enter the training data.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from rca_lab.mcp.claude_cli import run_claude
from rca_lab.mcp.codex_cli import run_codex

Provider = Literal["claude", "codex"]

JUDGE_SYSTEM_PROMPT = """You audit training data for a root-cause-analysis agent. Each item pairs the agent's private reasoning for one turn with the tool call the agent actually made right after it. Judge every item independently.

consistent = true only when the reasoning states an intention for the next step and that intention matches the actual call: the same tool (or an unmistakable description of it — "check its logs" matches a log-sampling tool, "look at the metric series" matches a time-series read) on the same target and the same kind of parameters. Set consistent = false when the reasoning announces a different tool or a different target, when it proposes nothing (a pure recap, a question with no decision), or when the stated plan and the call cannot both be true. A slightly different time window or limit is fine; a different target handle or a different tool family is not.

grounded = true when the reasoning relies only on the observations and alarms in the conversation. Set grounded = false when it cites guidance, feedback, a hint, a previous verdict, an evaluator, or knowledge it could not have from the conversation.

Reply with a JSON array only, no prose, one object per item in input order: {"id": <id>, "consistent": <bool>, "grounded": <bool>, "issue": "<at most 20 words, empty when both are true>"}."""


@dataclass
class JudgeConfig:
    provider: Provider = "claude"
    model: str = "claude-sonnet-5"
    claude_model: str | None = None
    batch_size: int = 12
    reasoning_chars: int = 1800
    timeout_seconds: int = 600
    retries: int = 2

    def __post_init__(self) -> None:
        if self.claude_model is not None:
            self.model = self.claude_model


Judge = Callable[[list[dict[str, Any]]], list[dict[str, Any]]]


def _run_provider(provider: Provider, *, system_prompt: str, prompt: str, model: str, timeout_seconds: int) -> tuple[str | None, str]:
    if provider == "claude":
        return run_claude(system_prompt=system_prompt, prompt=prompt, model=model, timeout_seconds=timeout_seconds)
    if provider == "codex":
        return run_codex(system_prompt=system_prompt, prompt=prompt, model=model, timeout_seconds=timeout_seconds)
    raise ValueError(f"unsupported judge provider: {provider}")


def _item(index: int, message: dict[str, Any], reasoning_chars: int) -> dict[str, Any]:
    call = message["tool_calls"][0]["function"]
    arguments = call.get("arguments") or "{}"
    if not isinstance(arguments, str):
        arguments = json.dumps(arguments, ensure_ascii=False)
    reasoning = (message.get("reasoning_content") or "").strip()
    if len(reasoning) > reasoning_chars:
        reasoning = reasoning[-reasoning_chars:]  # the decision is at the end
    return {"id": index, "reasoning": reasoning, "actual_call": {"tool": call["name"], "arguments": arguments}}


def judge_items(messages: list[dict[str, Any]], *, only: set[int] | None = None, reasoning_chars: int = 1800) -> list[dict[str, Any]]:
    """Judge inputs for every assistant tool-call turn (or the `only` indices) that has reasoning."""
    items = []
    for index, message in enumerate(messages):
        if message.get("role") != "assistant" or not message.get("tool_calls"):
            continue
        if only is not None and index not in only:
            continue
        if not (message.get("reasoning_content") or "").strip():
            continue
        items.append(_item(index, message, reasoning_chars))
    return items


def _extract_json_array(text: str) -> list[dict[str, Any]]:
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end < start:
        raise ValueError("judge reply has no JSON array")
    payload = json.loads(text[start : end + 1])
    if not isinstance(payload, list):
        raise TypeError("judge reply is not a list")
    return payload


def claude_judge(config: JudgeConfig) -> Judge:
    """An LLM-backed Judge (Claude by default, Codex when config.provider="codex"). Items are sent in
    batches; a batch whose reply cannot be parsed is retried, then its items are reported as
    `{"error": ...}` verdicts (treated as not judged, never as consistent)."""

    def run(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        verdicts: dict[int, dict[str, Any]] = {}
        for offset in range(0, len(items), config.batch_size):
            batch = items[offset : offset + config.batch_size]
            prompt = json.dumps({"items": batch}, ensure_ascii=False)
            last_error = ""
            for _ in range(config.retries + 1):
                result, last_error = _run_provider(config.provider, system_prompt=JUDGE_SYSTEM_PROMPT, prompt=prompt, model=config.model, timeout_seconds=config.timeout_seconds)
                if result is None:
                    continue
                try:
                    parsed = {int(v["id"]): v for v in _extract_json_array(result) if isinstance(v, dict) and "id" in v}
                except (json.JSONDecodeError, ValueError, KeyError, TypeError) as exc:
                    last_error = str(exc)
                    continue
                for item in batch:
                    verdict = parsed.get(int(item["id"]))
                    if verdict is None:
                        verdicts[int(item["id"])] = {"id": item["id"], "error": "missing from judge reply"}
                    elif not all(isinstance(verdict.get(field), bool) for field in ("consistent", "grounded")):
                        verdicts[int(item["id"])] = {"id": item["id"], "error": "judge verdict requires boolean consistent and grounded"}
                    else:
                        verdicts[int(item["id"])] = {"id": item["id"], "consistent": verdict["consistent"], "grounded": verdict["grounded"], "issue": str(verdict.get("issue", ""))[:200]}
                break
            else:
                for item in batch:
                    verdicts[int(item["id"])] = {"id": item["id"], "error": f"judge failed: {last_error[:200]}"}
        return [verdicts[int(item["id"])] for item in items]

    return run


def flagged(verdicts: list[dict[str, Any]]) -> list[int]:
    """Turn indices whose reasoning must not be trusted: inconsistent, ungrounded, or unjudged."""
    return sorted(int(v["id"]) for v in verdicts if v.get("error") or not v.get("consistent") or not v.get("grounded", True))
