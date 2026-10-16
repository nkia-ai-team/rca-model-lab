"""Snapshot train-only rationalized turns for a separate observation-support audit."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from rca_lab.mcp.evidence_audit import build_evidence_item
from rca_lab.mcp.export import view_source
from rca_lab.mcp.rationalize import infrastructure_pending


def canonical_json(value: object) -> str:
    """Same per-item JSON representation used by the evidence budget."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def train_members(split: dict) -> set[str]:
    train = split.get("train")
    if not isinstance(train, list) or not train or not all(isinstance(x, str) for x in train):
        raise ValueError("split requires nonempty string train list")
    for name, values in split.items():
        if name != "train" and isinstance(values, list) and set(train).intersection(values):
            raise ValueError(f"train overlaps {name}")
    return set(train)


def snapshot(sources: Path, split_path: Path, output: Path, *, cases: int = 6,
             max_chars: int = 120000, selection: str = "phases",
             exclude_cases: set[str] | None = None) -> dict:
    if selection not in {"phases", "final_and_rejected"}:
        raise ValueError("unknown selection")
    if cases < 1:
        raise ValueError("cases must be positive")
    if output.exists():
        raise FileExistsError("use a new output directory; snapshots are immutable")
    split_raw = split_path.read_bytes()
    train = train_members(yaml.safe_load(split_raw))
    selected = []
    seen = set()
    skipped: dict[str, int] = {}
    for stats_path in sorted(sources.glob("*/case-*/run*/rationalize.json")):
        run = stats_path.parent
        case = run.parent.name
        if case not in train or case in seen or case in (exclude_cases or set()):
            continue
        try:
            trajectory = run / "trajectory.rationalized.jsonl"
            a, b = stats_path.read_bytes(), trajectory.read_bytes()
            stats = json.loads(a)
            score = json.loads((run / "score.json").read_text())
            if score.get("case_id") != case:
                raise ValueError("case_identity_mismatch")
            messages = [json.loads(line) for line in b.splitlines() if line.strip()]
            stamp = view_source(run)
            if stats.get("view_source") != stamp:
                raise ValueError("stale_view")
            if (stats.get("judge_provider"), stats.get("judge_model")) != ("codex", "gpt-6.1-sol"):
                raise ValueError("different_judge")
            if infrastructure_pending(messages, stats):
                raise ValueError("incomplete")
            if a != stats_path.read_bytes() or b != trajectory.read_bytes() or stamp != view_source(run):
                raise ValueError("source_changed")
            turns = [i for i, m in enumerate(messages) if m.get("role") == "assistant"
                     and m.get("tool_calls") and m.get("reasoning_content")]
            if not turns:
                raise ValueError("no_turns")
            items = []
            positions = sorted({0, len(turns) // 2, len(turns) - 1})
            if selection == "final_and_rejected":
                finals = [pos for pos, idx in enumerate(turns) if any(
                    call.get("function", {}).get("name") == "submit_rca"
                    for call in messages[idx].get("tool_calls", []))]
                rejected = [pos for pos, idx in enumerate(turns) if any(
                    stats["judge"][str(idx)].get(key) is False
                    for key in ("consistent", "grounded"))]
                if not finals:
                    raise ValueError("no_final_submission")
                positions = sorted(set([finals[-1]] + rejected[:1]))
            for position in positions:
                index = turns[position]
                item = build_evidence_item(messages, index, max_chars=max_chars)
                items.append({"case_id": case, "turn_index": index, "item": item,
                              "legacy_verdict": stats["judge"].get(str(index)),
                              "phase": "final_submission" if selection == "final_and_rejected" and position == finals[-1] else
                              "legacy_rejected" if selection == "final_and_rejected" else
                              "early" if position == 0 else
                              "final" if position == len(turns) - 1 else "middle"})
            selected.append((run, a, b, stamp, items))
            seen.add(case)
        except (ValueError, KeyError, OSError) as exc:
            reason = str(exc)[:120]
            skipped[reason] = skipped.get(reason, 0) + 1
        if len(seen) >= cases:
            break
    if not selected:
        raise ValueError(f"no eligible completed train runs: {skipped}")
    output.mkdir(parents=True)
    (output / "split.yaml").write_bytes(split_raw)
    entries = []
    for ordinal, (run, a, b, stamp, items) in enumerate(selected):
        snap = output / "snapshots" / f"{ordinal:02d}"
        snap.mkdir(parents=True)
        (snap / "rationalize.json").write_bytes(a)
        (snap / "trajectory.rationalized.jsonl").write_bytes(b)
        for row in items:
            row.update({"source": str(run.resolve()), "trajectory_sha256": sha(b),
                        "stats_sha256": sha(a), "view_source": stamp})
            row["audit_id"] = f"{ordinal:02d}-{row['turn_index']}"
            row["item_sha256"] = sha(json.dumps(row["item"], ensure_ascii=False, sort_keys=True).encode())
            entries.append(row)
    (output / "items.jsonl").write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in entries))
    # Blind judge file excludes prior verdict, reference answer, case identity and source paths.
    blind = [{"audit_id": x["audit_id"], "item": x["item"]} for x in entries]
    (output / "blind-items.json").write_text(canonical_json(blind))
    sizes = [len(canonical_json(x["item"])) for x in entries]
    report = {"status": "inputs_prepared_not_judged", "cases": len(seen), "items": len(entries),
              "split_sha256": sha(split_raw), "requested_cases": cases, "skipped": skipped,
              "max_chars": max_chars, "size_unit": "compact per-item JSON characters, not tokens; envelope/prompt excluded",
              "item_chars_min": min(sizes), "item_chars_max": max(sizes),
              "item_chars_total": sum(sizes), "sampling": f"sorted first eligible run/case; {selection}; exploratory, not random",
              "excluded_cases": sorted(exclude_cases or set()),
              "context_policy_sha256": sha((ROOT / "src/rca_lab/mcp/context.py").read_bytes()),
              "audit_code_sha256": sha((ROOT / "src/rca_lab/mcp/evidence_audit.py").read_bytes())}
    (output / "manifest.json").write_text(json.dumps(report, indent=2))
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sources", type=Path, required=True)
    parser.add_argument("--split", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cases", type=int, default=6)
    parser.add_argument("--max-chars", type=int, default=120000)
    parser.add_argument("--selection", choices=["phases", "final_and_rejected"], default="phases")
    parser.add_argument("--exclude-cases", type=Path, help="JSON list of prior pilot cases")
    args = parser.parse_args()
    print(json.dumps(snapshot(args.sources, args.split, args.output, cases=args.cases,
                              max_chars=args.max_chars, selection=args.selection,
                              exclude_cases=set(json.loads(args.exclude_cases.read_text()))
                              if args.exclude_cases else None), indent=2))


if __name__ == "__main__":
    main()
