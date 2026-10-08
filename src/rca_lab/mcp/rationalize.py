"""STaR-style rationalization: regenerate the student's reasoning under the *clean* prompt while
teacher-forcing the accepted action of every turn.

Why: a hinted episode reaches the right answer, but the student's reasoning in that episode talks
about the guidance ("Feedback says the previous investigation…"). Training on that reasoning with
a clean prompt would teach the model to hallucinate guidance. Here each turn is replayed with the
context the student sees at inference (no hint, prior reasoning stripped) and the grammar allows
only the original tool call; the model writes fresh reasoning that can only draw on observations.
The same works for teacher (Claude) action sequences rebuilt into the student format.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

from rca_lab.mcp.claude_cli import SESSION_LIMIT_RE, SessionLimitError
from rca_lab.mcp.judge import Judge
from rca_lab.mcp.muse_grammar import exact_call_request_fields
from rca_lab.mcp.prompts import action_note
from rca_lab.mcp.rationale_writer import WriterConfig, write_rationale
from rca_lab.mcp.student import _post_chat, _strip_prior_reasoning_wire


@dataclass
class RationalizeConfig:
    endpoint: str
    model: str
    max_tokens: int = 1200  # caps the private reasoning: a forced call that does not fit is retried, so targets stay concise
    temperature: float = 0.7
    top_p: float = 0.95
    top_k: int = 64
    seed: int = 0
    request_timeout: int = 900
    max_attempts: int = 4  # per turn: the forced call must parse back identical (context shrinks count)
    action_note: bool = True  # student writer: tell the model (live prompt only) which call the reasoning must lead to
    writer: str = "student"  # "student" (vLLM, forced call), "claude", or "codex" (rationale_writer.py)
    writer_model: str = "claude-sonnet-5"
    reasoning_guard: bool = True  # student writer: no ATEM markup inside the reasoning (muse_grammar)


class RationalizeInterrupted(SessionLimitError):
    """Rationalization stopped by quota after making resumable partial progress."""

    def __init__(self, reason: str, messages: list[dict[str, Any]], stats: dict[str, Any]) -> None:
        super().__init__(reason)
        self.messages = messages
        self.stats = stats


def _call_of(message: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    call = message["tool_calls"][0]["function"]
    arguments = call.get("arguments") or "{}"
    return call["name"], (json.loads(arguments) if isinstance(arguments, str) else dict(arguments))


def _parsed_call(reply: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    calls = reply["choices"][0]["message"].get("tool_calls") or []
    if not calls:
        return None
    function = calls[0]["function"]
    arguments = function.get("arguments") or "{}"
    return function["name"], (json.loads(arguments) if isinstance(arguments, str) else dict(arguments))


def tool_turn_indices(messages: list[dict[str, Any]]) -> list[int]:
    """Assistant turn indices that carry a forced tool call."""
    return [index for index, message in enumerate(messages) if message.get("role") == "assistant" and message.get("tool_calls")]


def quota_contaminated_reasoning(message: dict[str, Any]) -> bool:
    """True when a legacy cached reasoning field is actually a Claude quota notice."""
    reasoning = (message.get("reasoning_content") or message.get("reasoning") or "").strip()
    return bool(reasoning and SESSION_LIMIT_RE.fullmatch(reasoning))


def _valid_judge_verdict(verdict: Any) -> bool:
    if not isinstance(verdict, dict) or verdict.get("error"):
        return False
    if not isinstance(verdict.get("consistent"), bool):
        return False
    return isinstance(verdict.get("grounded"), bool)


def rationalize_infra_failures(messages: list[dict[str, Any]], stats: dict[str, Any], *, judge_expected: bool) -> list[int]:
    """Turn indices whose cached rationalization is incomplete because infrastructure failed.

    This is deliberately narrower than "content rejected by the judge". A valid verdict with
    consistent=false/grounded=false is a data-quality rejection; missing reasoning, quota notices,
    missing verdicts, and judge errors are resumable infrastructure failures.
    """
    failed = {int(index) for index in stats.get("failed", [])}
    for index in tool_turn_indices(messages):
        message = messages[index]
        if not (message.get("reasoning_content") or "").strip() or quota_contaminated_reasoning(message):
            failed.add(index)
    if judge_expected:
        raw = stats.get("judge")
        verdicts = {int(k): v for k, v in raw.items()} if isinstance(raw, dict) else {}
        if not verdicts:
            failed.update(tool_turn_indices(messages))
        else:
            for index in tool_turn_indices(messages):
                if not (messages[index].get("reasoning_content") or "").strip():
                    continue
                verdict = verdicts.get(index)
                if not _valid_judge_verdict(verdict):
                    failed.add(index)
    return sorted(failed)


def infrastructure_pending(messages: list[dict[str, Any]], stats: dict[str, Any]) -> list[int]:
    """Cached rationalized turn indices still blocked by writer/judge infrastructure."""
    return rationalize_infra_failures(messages, stats, judge_expected=True)


def repair_turns(config: RationalizeConfig, out: list[dict[str, Any]], indices: set[int], tools: list[dict[str, Any]], stats: dict[str, Any]) -> list[int]:
    """Regenerate only selected turns and keep stats["failed"] aligned with the result."""
    failed = {int(index) for index in stats.get("failed", [])}
    verdicts = stats.get("judge") if isinstance(stats.get("judge"), dict) else {}
    repaired: list[int] = []
    for index in sorted(indices):
        failed.add(index)
        verdicts.pop(str(index), None)
        stats["failed"] = sorted(failed)
        try:
            ok = regenerate_turn(config, out, index, tools, stats)
        except SessionLimitError as exc:
            raise RationalizeInterrupted(str(exc), out, stats) from exc
        if ok:
            repaired.append(index)
            failed.discard(index)
            stats["regenerated"] = int(stats.get("regenerated", 0)) + 1
        if verdicts:
            stats["judge"] = verdicts
        stats["failed"] = sorted(failed)
    return repaired


def regenerate_turn(config: RationalizeConfig, out: list[dict[str, Any]], index: int, tools: list[dict[str, Any]], stats: dict[str, Any], *, seed_offset: int = 0) -> bool:
    """Resample the reasoning of assistant turn `index` in place (its call is forced). Returns
    whether a sample parsed back identical and non-empty; on failure the turn keeps no reasoning."""
    message = out[index]
    name, arguments = _call_of(message)
    context = _strip_prior_reasoning_wire(out[:index])
    if config.writer in {"claude", "codex"}:
        reasoning = write_rationale(WriterConfig(provider=config.writer, model=config.writer_model), context, name, arguments)
        if reasoning:
            message["reasoning_content"] = reasoning
            return True
        # A writer outage must not destroy the text the turn already has: it stays, the judge keeps
        # it flagged, and a later repair pass can replace it. (A run that wiped 45% of all turns
        # during a session-limit window is why this is not a `pop`.)
        return False
    if config.action_note:
        # The reasoning must know the call it has to lead to (otherwise it is written blind and
        # the grammar just forces an unrelated call after it — 11/12 turns inconsistent, measured).
        # As a system-prompt suffix the note was ignored (11/12 still inconsistent); it goes in
        # as the last message, right before generation. Live prompt only; never stored.
        context.append({"role": "user", "content": action_note(name, arguments).strip()})
    for attempt in range(config.max_attempts):
        body: dict[str, Any] = {
            "model": config.model,
            "messages": context,
            "tools": tools,
            # Late turns of long episodes need more room: the cap relaxes on later attempts
            # (1×, 1×, 1.5×, 2×) so concise reasoning is preferred but not forced.
            "max_tokens": int(config.max_tokens * (1.0, 1.0, 1.5, 2.0)[min(attempt, 3)]),
            "temperature": config.temperature,
            "top_p": config.top_p,
            "top_k": config.top_k,
            "seed": config.seed + 97 * index + attempt + seed_offset,
            "stream": False,
            **exact_call_request_fields(name, arguments, reasoning_guard=config.reasoning_guard),
        }
        try:
            reply = _post_chat(config, body)  # type: ignore[arg-type]
        except RuntimeError as exc:
            if "maximum context length" not in str(exc):
                raise
            # The rolling view already bounds the context; a turn that still overflows is
            # beyond the window the student is trained for — leave it without reasoning.
            stats["overflow"] = stats.get("overflow", 0) + 1
            break
        parsed = _parsed_call(reply)
        reasoning = reply["choices"][0]["message"].get("reasoning_content") or reply["choices"][0]["message"].get("reasoning") or ""
        if parsed is not None and parsed[0] == name and parsed[1] == arguments and reasoning.strip():
            message["reasoning_content"] = reasoning.strip()
            return True
    return False


def judge_pass(config: RationalizeConfig, out: list[dict[str, Any]], tools: list[dict[str, Any]], judge: Judge, stats: dict[str, Any], *, rounds: int, only: set[int] | None = None) -> None:
    """Judge the reasoning of every tool-call turn (or `only`), resample the flagged ones and
    judge again, up to `rounds` resamples. Records per-turn verdicts in stats["judge"] and the
    turns still flagged (inconsistent / ungrounded / unjudged / without reasoning) in
    stats["inconsistent"] — those must not be supervised."""
    from rca_lab.mcp.judge import flagged, judge_items

    existing = stats.get("judge") if isinstance(stats.get("judge"), dict) else {}
    verdicts: dict[int, dict[str, Any]] = {int(k): v for k, v in existing.items()}
    items = judge_items(out, only=only)
    try:
        judged = judge(items)
    except SessionLimitError as exc:
        raise RationalizeInterrupted(str(exc), out, stats) from exc
    for verdict in judged:
        verdicts[int(verdict["id"])] = verdict
    missing = [index for index, message in enumerate(out) if message.get("role") == "assistant" and message.get("tool_calls") and not (message.get("reasoning_content") or "").strip() and (only is None or index in only)]
    latest = [verdicts[int(item["id"])] for item in items if int(item["id"]) in verdicts]
    history = [sorted(set(flagged(latest)) | set(missing))]  # writer failures are resampled too
    for round_index in range(rounds):
        todo = history[-1]
        if not todo:
            break
        resampled = [index for index in todo if regenerate_turn(config, out, index, tools, stats, seed_offset=1000 * (round_index + 1))]
        stats["judge_resampled"] = stats.get("judge_resampled", 0) + len(resampled)
        try:
            judged = judge(judge_items(out, only=set(resampled)))
        except SessionLimitError as exc:
            raise RationalizeInterrupted(str(exc), out, stats) from exc
        for verdict in judged:
            verdicts[int(verdict["id"])] = verdict
        history.append(flagged([verdicts[i] for i in todo if i in verdicts]) + [i for i in todo if i not in verdicts])
    no_reasoning = [index for index, message in enumerate(out) if message.get("role") == "assistant" and message.get("tool_calls") and not (message.get("reasoning_content") or "").strip() and (only is None or index in only)]
    previous_inconsistent = {int(index) for index in stats.get("inconsistent", [])}
    if only is None:
        previous_inconsistent = set()
    else:
        previous_inconsistent -= set(only)
    stats["judge"] = {str(k): v for k, v in sorted(verdicts.items())}
    stats["judge_rounds"] = [len(h) for h in history]
    stats["inconsistent"] = sorted(previous_inconsistent | set(history[-1]) | set(no_reasoning))


def rationalize_messages(config: RationalizeConfig, messages: list[dict[str, Any]], tools: list[dict[str, Any]], *, judge: Judge | None = None, judge_rounds: int = 2) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Return a copy of `messages` whose assistant turns carry regenerated reasoning_content.

    Every assistant turn must be a single tool call (the student format). Turns whose forced call
    does not parse back identical after `max_attempts` keep no reasoning and are reported. With a
    `judge`, every regenerated reasoning is checked against its call and resampled when it does
    not justify it (see judge.py); stats["inconsistent"] lists the turns that never passed.
    """
    out = [dict(message) for message in messages]
    indices = tool_turn_indices(out)
    for index in indices:
        out[index].pop("reasoning_content", None)
        out[index].pop("reasoning", None)
    stats: dict[str, Any] = {"turns": 0, "regenerated": 0, "failed": [], "seconds": 0.0, "writer_provider": config.writer, "writer_model": config.writer_model}
    started = time.time()
    for ordinal, index in enumerate(indices, start=1):
        stats["turns"] += 1
        try:
            ok = regenerate_turn(config, out, index, tools, stats)
        except SessionLimitError as exc:
            stats["failed"].append(index)
            stats["seconds"] = round(time.time() - started, 1)
            print(f"[rationalize] writer={config.writer} turn={ordinal}/{len(indices)} index={index} interrupted", flush=True)
            raise RationalizeInterrupted(str(exc), out, stats) from exc
        if ok:
            stats["regenerated"] += 1
            print(f"[rationalize] writer={config.writer} turn={ordinal}/{len(indices)} index={index} success", flush=True)
        else:
            stats["failed"].append(index)
            print(f"[rationalize] writer={config.writer} turn={ordinal}/{len(indices)} index={index} failed", flush=True)
    if judge is not None:
        try:
            judge_pass(config, out, tools, judge, stats, rounds=judge_rounds)
        except RationalizeInterrupted as exc:
            exc.stats["seconds"] = round(time.time() - started, 1)
            raise
    stats["seconds"] = round(time.time() - started, 1)
    return out, stats
