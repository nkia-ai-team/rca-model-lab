#!/usr/bin/env python3
"""Reattach lossless legacy teacher actions to their recorded runtime prompts.

Some early teacher collectors normalized actions before writing episode JSONL and
dropped renamed fields.  The companion actions JSON still contains the original
accepted response.  This migration pairs those responses with the exact recorded
system/user/observation turns; the SFT exporter then performs the typed wire-format
migration in one audited place.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def migrate(*, episode: Path, actions: Path, output: Path) -> None:
    turns: list[dict[str, Any]] = [
        json.loads(line) for line in episode.read_text(encoding="utf-8").splitlines() if line
    ]
    source_actions: list[dict[str, Any]] = json.loads(actions.read_text(encoding="utf-8"))
    if len(turns) != len(source_actions):
        raise ValueError(
            f"turn/action count mismatch: episode={len(turns)} actions={len(source_actions)}"
        )
    for index, (turn, action) in enumerate(zip(turns, source_actions, strict=True), 1):
        if turn.get("turn") != index:
            raise ValueError(f"episode turns must be contiguous: turn={turn.get('turn')}")
        if not isinstance(action, dict) or not action.get("action"):
            raise ValueError(f"legacy action {index} is invalid")
        turn["action"] = action
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        "".join(json.dumps(turn, ensure_ascii=False, separators=(",", ":")) + "\n" for turn in turns),
        encoding="utf-8",
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--episode", type=Path, required=True)
    parser.add_argument("--actions", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    migrate(episode=args.episode, actions=args.actions, output=args.output)


if __name__ == "__main__":
    main()
