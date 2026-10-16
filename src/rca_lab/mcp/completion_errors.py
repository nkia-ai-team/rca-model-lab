"""Typed completion failures; only explicit server capacity errors end a rollout."""
from __future__ import annotations

import json


class ChatCompletionError(RuntimeError):
    def __init__(self, status: int, detail: str):
        self.status = status
        self.detail = detail
        try:
            payload = json.loads(detail)
        except (ValueError, TypeError):
            payload = {}
        error = payload.get("error", {}) if isinstance(payload, dict) else {}
        self.error = error if isinstance(error, dict) else {}
        super().__init__(f"chat completion HTTP {status}: {detail[:500]}")

    @property
    def context_capacity_exceeded(self) -> bool:
        if self.status != 400:
            return False
        if self.error.get("code") == "context_length_exceeded":
            return True
        # vLLM provides a typed input_tokens error but no dedicated context code.
        # Require both its parameter and precise capacity diagnostic, not a keyword alone.
        return (
            self.error.get("type") == "BadRequestError"
            and self.error.get("param") == "input_tokens"
            and "maximum context length" in str(self.error.get("message", ""))
        )

    def as_record(self) -> dict:
        return {"http_status": self.status, "error": self.error,
                "context_capacity_exceeded": self.context_capacity_exceeded}
