"""One-shot `codex exec` calls for data-pipeline helpers.

The wrapper mirrors :func:`rca_lab.mcp.claude_cli.run_claude`: successful calls return the final
assistant text and failures return ``None`` with a diagnostic. Quota/usage limit notices raise
``SessionLimitError`` so callers can stop a shard instead of recording poisoned missing data.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import tempfile
from pathlib import Path

from rca_lab.mcp.claude_cli import SESSION_LIMIT_RE, SessionLimitError

_AUTH_ENV_PREFIXES = ("CODEX_", "OPENAI_")
_ENV_ALLOWLIST = {
    "HOME",
    "PATH",
    "LANG",
    "LC_ALL",
    "SSL_CERT_FILE",
    "REQUESTS_CA_BUNDLE",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
}
_FORBIDDEN_EVENT_MARKERS = ("tool", "command_execution", "file_change", "web_search")


def _codex_env() -> dict[str, str]:
    """Keep CLI auth/network basics while dropping workflow hook variables such as OMX_*."""
    return {
        key: value
        for key, value in os.environ.items()
        if key in _ENV_ALLOWLIST or key.startswith(_AUTH_ENV_PREFIXES)
    }


def _quota_notice(*parts: str) -> str | None:
    for part in parts:
        for line in part.splitlines():
            text = line.strip()
            if SESSION_LIMIT_RE.fullmatch(text):
                return text[:200]
            try:
                event = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                for value in _error_event_strings(event):
                    if SESSION_LIMIT_RE.search(value):
                        return value.strip()[:200]
    return None


def _flatten_strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        strings: list[str] = []
        for item in value.values():
            strings.extend(_flatten_strings(item))
        return strings
    if isinstance(value, list):
        strings = []
        for item in value:
            strings.extend(_flatten_strings(item))
        return strings
    return []


def _error_event_strings(event: dict[str, object]) -> list[str]:
    event_type = str(event.get("type", "")).lower()
    item = event.get("item")
    item_type = str(item.get("type", "")).lower() if isinstance(item, dict) else ""
    values: list[str] = []
    if event_type in {"error", "turn.failed"}:
        values.extend(_flatten_strings(event.get("message")))
        values.extend(_flatten_strings(event.get("error")))
        values.extend(_flatten_strings(event.get("text")))
    if isinstance(item, dict) and item_type == "error":
        values.extend(_flatten_strings(item))
    return values


def _json_error_summary(stdout: str) -> str:
    errors: list[str] = []
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        strings = _error_event_strings(event)
        if strings:
            errors.append("; ".join(strings)[:240])
        item = event.get("item")
        if isinstance(item, dict) and item.get("type") == "error":
            errors.append(str(item)[:240])
    return "; ".join(errors[:3])


def _tool_event_summary(stdout: str) -> str:
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        event_type = str(event.get("type", "")).lower()
        item = event.get("item")
        item_type = str(item.get("type", "")).lower() if isinstance(item, dict) else ""
        if any(marker in event_type or marker in item_type for marker in _FORBIDDEN_EVENT_MARKERS):
            return str(event)[:240]
    return ""


def run_codex(*, system_prompt: str, prompt: str, model: str, timeout_seconds: int) -> tuple[str | None, str]:
    """Return (final text, diagnostic). The text is None when Codex produced no final output."""
    temp_root = Path(tempfile.mkdtemp(prefix="rca-codex-"))
    with tempfile.NamedTemporaryFile(prefix="rca-codex-final-", delete=False) as output_file:
        output_path = Path(output_file.name)
    full_prompt = f"{system_prompt.rstrip()}\n\n{prompt}"
    command = [
        "codex",
        "exec",
        "--ignore-user-config",
        "--ignore-rules",
        "--ephemeral",
        "--skip-git-repo-check",
        "-C",
        str(temp_root),
        "-s",
        "read-only",
        "--json",
        "--color",
        "never",
        "-m",
        model,
        "-o",
        str(output_path),
        "-c",
        'shell_environment_policy.inherit="none"',
        "-c",
        "project_doc_max_bytes=0",
        "-c",
        'web_search="disabled"',
        "-c",
        "features.shell_tool=false",
        "-c",
        "features.unified_exec=false",
        "-c",
        "features.multi_agent=false",
        "-c",
        "features.apps=false",
        "-c",
        "features.plugins=false",
        "-",
    ]
    try:
        proc = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
            env=_codex_env(),
        )
        try:
            stdout, stderr = proc.communicate(full_prompt, timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                try:
                    proc.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
            return None, f"timeout after {timeout_seconds}s"
        quota = _quota_notice(stdout, stderr)
        if quota:
            raise SessionLimitError(quota)
        error = _json_error_summary(stdout)
        if error:
            return None, f"exit {proc.returncode}, codex error: {error!r}"
        if proc.returncode != 0:
            detail = stderr[:240] or stdout[:240]
            return None, f"exit {proc.returncode}, codex error: {detail!r}"
        tool_event = _tool_event_summary(stdout)
        if tool_event:
            return None, f"exit {proc.returncode}, unexpected codex tool event: {tool_event!r}"
        try:
            result = output_path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            return None, f"exit {proc.returncode}, final output unreadable: {exc}"
        quota = _quota_notice(result)
        if quota:
            raise SessionLimitError(quota)
        if not result:
            detail = _json_error_summary(stdout) or stderr[:160]
            return None, f"exit {proc.returncode}, empty final output: {detail!r}"
        return result, ""
    finally:
        try:
            output_path.unlink()
        except FileNotFoundError:
            pass
        shutil.rmtree(temp_root, ignore_errors=True)
