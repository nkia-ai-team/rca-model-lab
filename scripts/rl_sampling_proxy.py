#!/usr/bin/env python3
"""OpenAI-compatible proxy that enables diverse, reproducible RL rollouts."""

from __future__ import annotations

import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import Request, urlopen


class SamplingProxy(BaseHTTPRequestHandler):
    upstream: str
    temperature: float
    _seed = 0
    _lock = threading.Lock()

    @classmethod
    def next_seed(cls) -> int:
        with cls._lock:
            cls._seed += 1
            return cls._seed

    def _forward(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length) if length else None
        if body and self.path.endswith("/chat/completions"):
            payload = json.loads(body)
            payload["temperature"] = self.temperature
            payload["seed"] = self.next_seed()
            body = json.dumps(payload, ensure_ascii=False).encode()
        request = Request(
            f"{self.upstream}{self.path}",
            data=body,
            method=self.command,
            headers={"Content-Type": self.headers.get("Content-Type", "application/json")},
        )
        try:
            with urlopen(request, timeout=900) as response:
                content = response.read()
                self.send_response(response.status)
                self.send_header(
                    "Content-Type", response.headers.get("Content-Type", "application/json")
                )
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)
        except Exception as error:  # noqa: BLE001 - preserve upstream failure for the harness
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


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--listen", type=int, default=8003)
    parser.add_argument("--upstream", default="http://localhost:8002")
    # DAPO recomputes behavior-policy log probabilities from the frozen SFT
    # model. Temperature 1 keeps that denominator exactly reproducible.
    parser.add_argument("--temperature", type=float, default=1.0)
    parser.add_argument("--seed", type=int, default=1000)
    args = parser.parse_args()
    SamplingProxy.upstream = args.upstream.rstrip("/")
    SamplingProxy.temperature = args.temperature
    SamplingProxy._seed = args.seed
    ThreadingHTTPServer(("127.0.0.1", args.listen), SamplingProxy).serve_forever()


if __name__ == "__main__":
    main()
