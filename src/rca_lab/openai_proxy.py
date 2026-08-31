"""Small OpenAI-compatible request proxy used to enforce sampling contracts."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import Request, urlopen


def prepare_chat_payload(
    payload: dict[str, object],
    *,
    temperature: float,
    seed: int,
    reasoning_strength: str | None,
) -> dict[str, object]:
    """Apply one explicit sampling and chat-template contract."""
    prepared = dict(payload)
    prepared["temperature"] = temperature
    prepared["seed"] = seed
    if reasoning_strength is None:
        return prepared
    raw_kwargs = prepared.get("chat_template_kwargs")
    if raw_kwargs is None:
        raw_kwargs = {}
    if not isinstance(raw_kwargs, dict):
        raise TypeError("chat_template_kwargs must be an object")
    prepared["chat_template_kwargs"] = {
        **raw_kwargs,
        "reasoning_strength": reasoning_strength,
    }
    return prepared


def proxy_handler(
    *,
    upstream: str,
    temperature: float,
    initial_seed: int,
    reasoning_strength: str | None,
    increment_seed: bool,
) -> type[BaseHTTPRequestHandler]:
    """Build an isolated handler class so concurrent proxies never share state."""

    class OpenAIProxy(BaseHTTPRequestHandler):
        _seed = initial_seed
        _lock = threading.Lock()

        @classmethod
        def next_seed(cls) -> int:
            with cls._lock:
                seed = cls._seed
                if increment_seed:
                    cls._seed += 1
                return seed

        def _forward(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length) if length else None
            try:
                if body and self.path.endswith("/chat/completions"):
                    decoded = json.loads(body)
                    if not isinstance(decoded, dict):
                        raise ValueError("chat completion body must be an object")
                    decoded = prepare_chat_payload(
                        decoded,
                        temperature=temperature,
                        seed=self.next_seed(),
                        reasoning_strength=reasoning_strength,
                    )
                    body = json.dumps(decoded, ensure_ascii=False).encode()
                request = Request(
                    f"{upstream.rstrip('/')}{self.path}",
                    data=body,
                    method=self.command,
                    headers={
                        "Content-Type": self.headers.get(
                            "Content-Type", "application/json"
                        )
                    },
                )
                with urlopen(request, timeout=900) as response:
                    content = response.read()
                    self.send_response(response.status)
                    self.send_header(
                        "Content-Type",
                        response.headers.get("Content-Type", "application/json"),
                    )
                    self.send_header("Content-Length", str(len(content)))
                    self.end_headers()
                    self.wfile.write(content)
            except Exception as error:  # noqa: BLE001 - surface upstream failure to caller
                content = json.dumps({"error": str(error)}).encode()
                self.send_response(502)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)

        do_GET = _forward
        do_POST = _forward

        def log_message(self, format: str, *args: object) -> None:
            return

    return OpenAIProxy


@contextmanager
def enforced_openai_endpoint(
    upstream: str,
    *,
    temperature: float,
    seed: int,
    reasoning_strength: str | None,
    increment_seed: bool = False,
) -> Iterator[str]:
    """Serve an ephemeral loopback endpoint that enforces the request contract."""
    handler = proxy_handler(
        upstream=upstream,
        temperature=temperature,
        initial_seed=seed,
        reasoning_strength=reasoning_strength,
        increment_seed=increment_seed,
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
