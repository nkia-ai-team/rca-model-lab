"""Guided episodes -> training data: hinted student runs (hint ladder, see hint_ladder.py) and the
shared episode clean-up used for teacher runs (SFT export and the RL builder's --teacher-root).

A hinted run is usable only after three gates, in this order:
  1. accept   (scripts/mcp_ladder_accept.py): hinted, submitted, root_f1 = 1, answer refs grounded,
              mechanism judge = 1 (v2), and the final answer does not talk about the guidance.
              Writes review.json.
  2. rationalize (scripts/mcp_rationalize.py --student-root ... --student-require-review): every
              turn's reasoning is rewritten under the clean prompt for its forced call and judged;
              turns that never pass stay as context with supervise=false.
  3. consume  SFT export (--require-rationalized) or the RL builder (--hinted-root), which re-check
              the rationalized reasoning for guidance wording and the unsupervised-turn fraction.
The student's own reasoning in a hinted run echoes the live-prompt hint ("Previous feedback says
..."), so the raw trajectory.jsonl of a hinted run never reaches a loss.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

LEAK_RE = re.compile(r"가이드 피드백|오답 판정|이전 조사|피드백|previous investigation|feedback says|the feedback|guidance says", re.IGNORECASE)  # hint-specific wording only


def leaks_hint(messages: list[dict[str, Any]]) -> bool:
    """The assistant reasoning talks about the guidance."""
    return any(message.get("role") == "assistant" and LEAK_RE.search(message.get("reasoning_content") or "") for message in messages)


def answer_leaks_hint(result: dict[str, Any] | None) -> bool:
    """The submitted conclusion (kept verbatim by rationalize) talks about the guidance."""
    return bool(result) and bool(LEAK_RE.search(json.dumps(result, ensure_ascii=False)))


def mark_unsupervised(messages: list[dict[str, Any]], run_dir: Path) -> int:
    """Turns whose regenerated reasoning never passed the judge (rationalize.json "inconsistent")
    are kept as context but excluded from the loss (`supervise: false`, see mcp_sft.tokenize_turns).
    Returns how many turns were marked; -1 when the episode was never judged."""
    stats_path = run_dir / "rationalize.json"
    if not stats_path.exists():
        return -1
    stats = json.loads(stats_path.read_text(encoding="utf-8"))
    if "inconsistent" not in stats:
        return -1
    # A reviewer can also exclude turns whose *action* came from the live-prompt hint rather than from an
    # observation (review.json "unsupervise_turns": [message index, ...] with a reason).
    review_path = run_dir / "review.json"
    extra = json.loads(review_path.read_text(encoding="utf-8")).get("unsupervise_turns", []) if review_path.exists() else []
    marked = 0
    for index in sorted(set(stats["inconsistent"]) | set(extra)):
        if 0 <= index < len(messages) and messages[index].get("role") == "assistant":
            messages[index]["supervise"] = False
            marked += 1
    return marked


def reviewed(run_dir: Path) -> bool:
    path = run_dir / "review.json"
    return path.exists() and bool(json.loads(path.read_text(encoding="utf-8")).get("accepted"))


def ladder_verdict(score: dict[str, Any], result: dict[str, Any] | None, mechanism: float | None) -> tuple[bool, str]:
    """(accepted, reason) for one hint-ladder run; `mechanism` is the v2 judge grade."""
    checks = [
        (bool(score.get("hinted")), "not hinted"),
        (score.get("stop_reason") == "submitted", f"stop_reason={score.get('stop_reason')}"),
        (float(score.get("root_f1") or 0.0) == 1.0, f"root_f1={score.get('root_f1')}"),
        (float(score.get("ref_grounding") or 0.0) == 1.0, f"ref_grounding={score.get('ref_grounding')}"),
        (mechanism == 1.0, f"mechanism={mechanism}"),
        (not answer_leaks_hint(result), "final answer talks about the guidance"),
    ]
    failed = [reason for ok, reason in checks if not ok]
    if failed:
        return False, "auto-rejected (hint ladder): " + "; ".join(failed)
    return True, "auto-accepted (hint ladder): root_f1 1 + grounding 1 + mechanism 1 (v2) + clean answer"


def load_rationalized(run_dir: Path, *, max_unsupervised_fraction: float) -> tuple[list[dict[str, Any]] | None, str]:
    """(messages | None, reason): the rationalized trajectory with unsupervised marks, when every gate holds."""
    if not reviewed(run_dir):
        return None, "not accepted"
    path = run_dir / "trajectory.rationalized.jsonl"
    if not path.exists():
        return None, "not rationalized"
    messages = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    unsupervised = mark_unsupervised(messages, run_dir)
    if unsupervised < 0:
        return None, "rationalized reasoning never judged"
    turns = sum(1 for message in messages if message.get("role") == "assistant" and message.get("tool_calls"))
    if turns and unsupervised / turns > max_unsupervised_fraction:
        return None, f"{unsupervised}/{turns} turns failed the reasoning judge"
    if leaks_hint(messages):
        return None, "rationalized reasoning still talks about the guidance"
    return messages, "ok"


BUDGET_RE = re.compile(r"도구 호출은 최대 \d+회")


def clean_messages(messages: list[dict[str, Any]], tool_names: set[str], max_turns: int) -> tuple[list[dict[str, Any]], int]:
    """Drop assistant turns that call a tool outside the catalog (the teacher CLI records denied
    `Bash` attempts as tool_use + tool_use_error) together with their tool result, and rewrite the
    turn budget in the user prompt so every row states the budget the student is trained for.
    Returns (messages, dropped_turns)."""
    out: list[dict[str, Any]] = []
    dropped = 0
    skip_ids: set[str] = set()
    for message in messages:
        role = message.get("role")
        if role == "user":
            message = {**message, "content": BUDGET_RE.sub(f"도구 호출은 최대 {max_turns}회", str(message.get("content", "")))}
        elif role == "assistant" and message.get("tool_calls"):
            names = {call["function"]["name"] for call in message["tool_calls"]}
            if not names <= tool_names:
                skip_ids |= {call["id"] for call in message["tool_calls"]}
                dropped += 1
                continue
        elif role == "tool" and message.get("tool_call_id") in skip_ids:
            continue
        out.append(message)
    return out, dropped


def unsupervise_wrong_status(messages: list[dict[str, Any]], score: dict[str, Any]) -> int:
    """When the submitted status disagrees with the golden, keep the investigation but do not
    supervise the final submit_rca turn: the model still learns how the case was investigated,
    not the mis-graded conclusion. Teacher output is left unedited. Returns 1 when applied."""
    if score.get("status_correct"):
        return 0
    for message in reversed(messages):
        if message.get("role") == "assistant" and message.get("tool_calls") and message["tool_calls"][0]["function"]["name"] == "submit_rca":
            message["supervise"] = False
            return 1
    return 0
