"""Report hit and mechanism-strict accuracy without counting unjudged runs as failures.

Reads existing episodes only; JSON goes to stdout. Strict is null until every hit has a
valid judge verdict. Bounds include unresolved hits; non-hits are known strict failures.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from rca_lab.mcp.rl_reward import judge_cache_path, judge_cache_valid


def report(root: Path, *, model: str = "claude-opus-5-5", rubric: str = "v2", provider: str = "claude") -> dict:
    total = hits = judged = passed = pending = invalid = 0
    for score_path in sorted(root.glob("case-*/run*/score.json")):
        score = json.loads(score_path.read_text(encoding="utf-8"))
        if score.get("unscored"):
            continue
        total += 1
        if score.get("root_f1") != 1:
            continue
        hits += 1
        cache = judge_cache_path(score_path.parent, provider, model=model)
        if not cache.exists():
            pending += 1
            continue
        try:
            entry = json.loads(cache.read_text(encoding="utf-8"))
            valid = (
                isinstance(entry, dict)
                and type(entry.get("score")) in (int, float)
                and entry["score"] in (0, 0.5, 1)
                and judge_cache_valid(entry, score["case_id"], model=model, rubric=rubric, provider=provider)
            )
        except (ValueError, TypeError, KeyError):
            valid = False
        if not valid:
            invalid += 1
            continue
        judged += 1
        passed += entry["score"] == 1
    unresolved = pending + invalid
    return {
        "root": str(root),
        "model": model,
        "provider": provider,
        "rubric": rubric,
        "total": total,
        "hit": hits,
        "hit_rate": hits / total if total else None,
        "strict": passed if total and not unresolved else None,
        "strict_rate": passed / total if total and not unresolved else None,
        "strict_bounds": [passed, passed + unresolved] if total else None,
        "judged_hits": judged,
        "pending_hits": pending,
        "invalid_cache_hits": invalid,
        "unresolved_hits": unresolved,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("roots", nargs="+", type=Path)
    parser.add_argument("--model", default="claude-opus-5-5")
    parser.add_argument("--provider", choices=("claude", "codex"), default="claude")
    parser.add_argument("--rubric", default="v2")
    args = parser.parse_args()
    for root in args.roots:
        if not root.is_dir():
            parser.error(f"evaluation directory does not exist: {root}")
    print(json.dumps([report(root, model=args.model, rubric=args.rubric, provider=args.provider) for root in args.roots], indent=2))


if __name__ == "__main__":
    main()
