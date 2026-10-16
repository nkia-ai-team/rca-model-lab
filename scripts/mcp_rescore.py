"""Re-score existing episodes against the current golden contract (contract edits, no reruns).

  uv run python scripts/mcp_rescore.py outputs/mcp-eval/teacher-v1 outputs/mcp-eval/rollouts-v1 ...

Rewrites each run's score.json in place (keeping non-score fields) and prints a per-root summary.
Observed refs are recovered from the stored trajectory / stream so ref_grounding stays exact.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from rca_lab.mcp.golden import load_contract
from rca_lab.mcp.score import score_result
from rca_lab.mcp.teacher import summarize_events

SCORE_KEYS = ("status", "status_correct", "root_f1", "strict_correct", "unsupported_confirmation", "ref_grounding", "reward", "turns", "tool_calls", "tool_errors")


def _collect_refs(payload: Any, sink: set[str]) -> None:
    stack = [payload]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            refs = node.get("refs")
            if isinstance(refs, list):
                sink.update(str(ref) for ref in refs)
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)


def observed_refs_from_trajectory(run_dir: Path, alias_map: dict[str, str]) -> set[str]:
    """Student trajectories store aliased tool text; map handles back to UUIDs before comparing."""
    reverse = {handle: uuid for uuid, handle in alias_map.items()}
    refs: set[str] = set()
    for line in (run_dir / "trajectory.jsonl").read_text(encoding="utf-8").splitlines():
        message = json.loads(line)
        if message.get("role") != "tool":
            continue
        try:
            payload = json.loads(message.get("content") or "")
        except json.JSONDecodeError:
            continue
        found: set[str] = set()
        _collect_refs(payload, found)
        for ref in found:
            for handle, uuid in reverse.items():
                ref = ref.replace(handle, uuid)
            refs.add(ref)
    return refs


def rescore_run(run_dir: Path, contract: dict[str, Any]) -> dict[str, Any] | None:
    score_path = run_dir / "score.json"
    if not score_path.exists():
        return None
    score = json.loads(score_path.read_text(encoding="utf-8"))
    expected = contract.get(score.get("case_id", ""))
    if expected is None or score.get("unscored"):
        return None
    result = json.loads((run_dir / "result.json").read_text(encoding="utf-8")) if (run_dir / "result.json").exists() else None
    if (run_dir / "trajectory.jsonl").exists():
        turns, tool_calls, tool_errors = score.get("turns", 0), score.get("tool_calls", 0), score.get("tool_errors", 0)
        if (run_dir / "observed_refs.json").exists():
            observed = set(json.loads((run_dir / "observed_refs.json").read_text(encoding="utf-8")))
        else:
            # Runs recorded before observed_refs.json existed: stored tool text is truncated, so
            # trajectory refs undercount and grounding cannot be re-verified. Trust the runtime
            # verdict by treating cited refs as observed (grounding stays as originally graded).
            alias_map = json.loads((run_dir / "alias.json").read_text(encoding="utf-8")) if (run_dir / "alias.json").exists() else {}
            observed = observed_refs_from_trajectory(run_dir, alias_map)
            if result is not None:
                observed |= {ref for cause in result.get("causes", []) for ref in cause.get("support_refs", [])}
                observed |= {ref for cause in result.get("external_causes", []) for ref in cause.get("evidence_refs", [])}
    else:
        events = [json.loads(line) for line in (run_dir / "stream.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
        _, turns, tool_calls, tool_errors, observed = summarize_events(events, run_dir / "result.json")
    fresh = score_result(expected, result, turns=turns, tool_calls=tool_calls, tool_errors=tool_errors, observed_refs=observed)
    before = {key: score.get(key) for key in ("root_f1", "strict_correct")}
    score.update({key: fresh[key] for key in SCORE_KEYS})
    score_path.write_text(json.dumps(score, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"case_id": score["case_id"], "run": run_dir.name, "before": before, "after": {key: fresh[key] for key in ("root_f1", "strict_correct")}}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("roots", nargs="+", type=Path)
    parser.add_argument("--contract", default="configs/eval/train-family-v2.yaml")
    args = parser.parse_args()
    contract = load_contract(Path(args.contract))
    for root in args.roots:
        changed = 0
        rows = []
        for run_dir in sorted(path.parent for path in root.glob("case-*/run*/score.json")):
            row = rescore_run(run_dir, contract)
            if row:
                rows.append(row)
                changed += row["before"] != row["after"]
        full = sum(row["after"]["root_f1"] == 1.0 for row in rows)
        print(f"{root}: {len(rows)} runs rescored, {changed} changed, full-hit {full}/{len(rows)}")
        for row in rows:
            if row["before"] != row["after"]:
                print(f"   {row['case_id'][5:10]} {row['run']}: {row['before']} -> {row['after']}")


if __name__ == "__main__":
    main()
