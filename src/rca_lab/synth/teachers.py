"""프런티어 모델 CLI(claude / codex)를 서브프로세스로 호출하는 교사 인터페이스.

API 키 대신 각 CLI 의 로그인 세션을 그대로 쓴다. 새 교사를 붙이려면 Teacher 를 상속하고
TEACHERS 에 등록한다.
"""

from __future__ import annotations

import subprocess
import tempfile
from abc import ABC, abstractmethod
from pathlib import Path


class Teacher(ABC):
    name: str

    def __init__(self, model: str | None = None, timeout: int = 600):
        self.model = model
        self.timeout = timeout

    @abstractmethod
    def complete(self, prompt: str, system: str | None = None) -> str: ...


class ClaudeTeacher(Teacher):
    name = "claude"

    def complete(self, prompt: str, system: str | None = None) -> str:
        cmd = ["claude", "-p", "--output-format", "text", "--tools", ""]
        if self.model:
            cmd += ["--model", self.model]
        if system:
            cmd += ["--system-prompt", system]
        r = subprocess.run(
            cmd, input=prompt, text=True, capture_output=True, timeout=self.timeout, check=False
        )
        if r.returncode != 0:
            raise RuntimeError(f"claude failed ({r.returncode}): {r.stderr[-2000:]}")
        return r.stdout.strip()


class CodexTeacher(Teacher):
    name = "codex"

    def complete(self, prompt: str, system: str | None = None) -> str:
        full = f"{system}\n\n{prompt}" if system else prompt
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "last.md"
            cmd = [
                "codex",
                "exec",
                "--skip-git-repo-check",
                "--ephemeral",
                "-s",
                "read-only",
                "-C",
                td,
                "-o",
                str(out),
                "-",
            ]
            if self.model:
                cmd += ["-m", self.model]
            r = subprocess.run(
                cmd, input=full, text=True, capture_output=True, timeout=self.timeout, check=False
            )
            if r.returncode != 0 or not out.exists():
                raise RuntimeError(f"codex failed ({r.returncode}): {r.stderr[-2000:]}")
            return out.read_text().strip()


TEACHERS: dict[str, type[Teacher]] = {t.name: t for t in (ClaudeTeacher, CodexTeacher)}


def get_teacher(name: str, **kw) -> Teacher:
    try:
        return TEACHERS[name](**kw)
    except KeyError:
        raise ValueError(f"unknown teacher {name!r}; choose from {sorted(TEACHERS)}") from None
