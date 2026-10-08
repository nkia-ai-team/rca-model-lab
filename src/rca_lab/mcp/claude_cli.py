"""One-shot `claude -p` calls for data-pipeline helpers (judge, rationale writer).

Both helpers send a prompt on stdin with every built-in tool denied and read the JSON envelope's
`result` text. The account's quota limit surfaces there as ordinary result text ("You've hit your
session limit · resets 5:20am (UTC)"), not as an error exit — treating that as a per-item failure
silently destroys a whole run, so it is raised as `SessionLimitError` and the caller stops.
"""

from __future__ import annotations

import json
import re
import subprocess

# Match a whole CLI notice, never quota-related words inside an ordinary model answer.
SESSION_LIMIT_RE = re.compile(
    r"(?:you(?:'ve| have) hit your (?:session|usage|weekly) limit|rate limit exceeded)"
    r"(?:\s*[·:.-]\s*resets? [^\n]+)?[.!]?",
    re.IGNORECASE,
)

DENY_BUILTINS = "Bash,Read,Edit,Write,MultiEdit,Glob,Grep,LS,WebFetch,WebSearch,Agent,Task,NotebookEdit,TodoWrite"


class SessionLimitError(RuntimeError):
    """The Claude CLI reported the account's session/usage/weekly limit. Retrying now is pointless."""


def run_claude(*, system_prompt: str, prompt: str, model: str, timeout_seconds: int) -> tuple[str | None, str]:
    """Return (result text, diagnostic). The text is None when the call produced no result."""
    try:
        proc = subprocess.run(
            ["claude", "-p", "--model", model, "--output-format", "json", "--no-session-persistence", "--setting-sources", "", "--tools", "", "--disallowedTools", DENY_BUILTINS, "--system-prompt", system_prompt],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        # A hung call is an ordinary failed call (callers retry or record the failure); raising here
        # killed a whole rationalize shard mid-run (2026-10-06).
        return None, f"timeout after {timeout_seconds}s"
    try:
        envelope = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None, f"exit {proc.returncode}, stdout not JSON: {proc.stdout[:160]!r} stderr={proc.stderr[:160]!r}"
    result = envelope.get("result") if isinstance(envelope, dict) else None
    # Explicit success metadata wins; older CLI envelopes may omit is_error.
    if (
        isinstance(result, str)
        and envelope.get("is_error") is not False
        and SESSION_LIMIT_RE.fullmatch(result.strip())
    ):
        raise SessionLimitError(result.strip()[:200])
    if isinstance(envelope, dict) and envelope.get("is_error") is True:
        return None, f"exit {proc.returncode}, CLI error: {result!r}"
    if not isinstance(result, str) or not result.strip():
        subtype = envelope.get("subtype") if isinstance(envelope, dict) else "?"
        return None, f"exit {proc.returncode}, subtype={subtype}, stderr={proc.stderr[:160]!r}"
    return result.strip(), ""
