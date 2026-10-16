"""Replay accepted teacher runs against the current rca-mcp binary (2026-10-06): the blind sanitizer used to
re-sort every envelope alphabetically (summary pushed past the student's 6000-char view), so trajectories collected
before the fix carry tool responses the student will never see at evaluation. Replaying re-issues each recorded call
(same tool, same arguments, same capture) and writes `ledger.replay.jsonl` next to the original stream; the export
path prefers it (rca_lab.mcp.export.load_teacher_turns). The teacher's reasoning and call sequence are unchanged.

  uv run python scripts/mcp_replay_teacher.py --root outputs/mcp-eval/rcae-tt4-teacher --cases-root data/external/rcaeval/captures --project-prefix rp0
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import mcp_case_run

from rca_lab.mcp.answer import SUBMIT_TOOL_NAME
from rca_lab.mcp.client import MCPClient
from rca_lab.mcp.export import _turns_from_stream

REPLAY = "ledger.replay.jsonl"


def accepted(run: Path) -> bool:
    review = run / "review.json"
    return review.exists() and bool(json.loads(review.read_text(encoding="utf-8")).get("accepted"))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", action="append", required=True, type=Path)
    parser.add_argument("--cases-root", type=Path, required=True)
    parser.add_argument("--mcp-binary", type=Path, default=ROOT / "tools/rca-mcp/bin/rca-mcp")
    parser.add_argument("--project-prefix", default="rp")
    parser.add_argument("--shard", default="0/1", help="k/n over capture ids")
    parser.add_argument("--all-runs", action="store_true", help="also replay runs that were not accepted")
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    k, n = (int(x) for x in args.shard.split("/"))
    sha = hashlib.sha256(args.mcp_binary.read_bytes()).hexdigest()[:12]
    by_case: dict[str, list[Path]] = {}
    for root in args.root:
        for stream in sorted(root.glob("case-*/run*/stream.jsonl")):
            run = stream.parent
            if (args.all_runs or accepted(run)) and (args.overwrite or not (run / REPLAY).exists()):
                by_case.setdefault(run.parent.name, []).append(run)
    cases = sorted(by_case)
    cases = [c for i, c in enumerate(cases) if i % n == k]
    print(f"[replay] {len(cases)} captures, {sum(len(by_case[c]) for c in cases)} runs, binary {sha}", flush=True)
    for case_id in cases:
        runs = by_case[case_id]
        work = Path(os.environ.get("REPLAY_WORK", "/tmp/claude-1006/-home-swlee-projects/47c4a666-edf6-440b-93bc-c522a28efa1a/scratchpad/replay-work")) / f"{case_id}-{int(time.time())}"
        work.mkdir(parents=True, exist_ok=True)
        project = f"{args.project_prefix}-" + case_id.split("-v3-")[0].replace("case-", "")
        started = time.time()
        try:
            connections = mcp_case_run.restore_case(args.cases_root / case_id, project, work / "connections.json", work / "restore.log")
            connections = {**connections, **mcp_case_run.external_capture_env(args.cases_root / case_id)}
            for run in runs:
                seed_path = run.parent / "seed.json" if (run.parent / "seed.json").exists() else run / "seed.json"
                window = json.loads(seed_path.read_text(encoding="utf-8"))["time_window"]
                events = [json.loads(line) for line in (run / "stream.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
                turns = _turns_from_stream(events)
                command = [str(args.mcp_binary), "-blind", "-first-event", window["first_event"], "-last-event", window["last_event"]]
                records = []
                with MCPClient(command, {**os.environ, **connections}, log_path=work / "mcp.stderr") as mcp:
                    for turn in turns:
                        if turn["name"] == SUBMIT_TOOL_NAME:
                            records.append({"kind": "submit", "thought": turn.get("thought", ""), "arguments": turn["arguments"]})
                            continue
                        text, is_error = mcp.call(turn["name"], turn["arguments"])
                        records.append({"kind": "call", "thought": turn.get("thought", ""), "name": turn["name"], "arguments": turn["arguments"],
                                        "response": text, "is_error": is_error, "original_is_error": turn.get("is_error", False)})
                (run / REPLAY).write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records), encoding="utf-8")
                (run / "replay.meta.json").write_text(json.dumps({"binary_sha256_12": sha, "replayed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "calls": sum(r["kind"] == "call" for r in records),
                                                                  "errors_now": sum(bool(r.get("is_error")) for r in records), "errors_then": sum(bool(r.get("original_is_error")) for r in records)}, indent=1), encoding="utf-8")
            print(f"[replay] {case_id}: {len(runs)} run(s) in {time.time() - started:.0f}s", flush=True)
        except Exception as exc:  # one broken capture must not stop the shard
            print(f"[replay] {case_id}: FAILED {type(exc).__name__}: {str(exc)[:200]}", flush=True)
        finally:
            mcp_case_run.teardown(project)


if __name__ == "__main__":
    main()
