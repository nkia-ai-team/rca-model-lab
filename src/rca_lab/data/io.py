from __future__ import annotations

from collections.abc import Iterable, Iterator
from pathlib import Path

from .schema import RcaSample


def write_jsonl(path: Path, samples: Iterable[RcaSample]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with path.open("w", encoding="utf-8") as f:
        for s in samples:
            f.write(s.model_dump_json() + "\n")
            n += 1
    return n


def read_jsonl(path: Path) -> Iterator[RcaSample]:
    with path.open(encoding="utf-8") as f:
        for line in f:
            if line.strip():
                yield RcaSample.model_validate_json(line)
