"""Continue a rejected submission in a separate artifact, without database access.

This is a format-recovery experiment, not a replacement benchmark result. Golden
answers are loaded only after inference has returned, for deterministic scoring.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from rca_lab.mcp.student import StudentConfig, run_forced_submit_repair


def read(path: Path):
    return json.loads(path.read_text())


def write(path: Path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--retry-budget", type=int, default=1)
    args = parser.parse_args()
    source = args.source.resolve()
    destination = args.destination.resolve()
    original = read(source / "score.json")
    if original["stop_reason"] != "forced_answer_rejected":
        parser.error("source must be a rejected forced submission")
    if destination.exists() or source == destination:
        parser.error("destination must be new; original artifacts cannot be replaced")
    plan = read(args.plan)
    messages = [json.loads(line) for line in (source / "trajectory.jsonl").read_text().splitlines() if line.strip()]
    aliases = read(source / "alias.json")
    refs = set(read(source / "observed_refs.json"))
    files = ["trajectory.jsonl", "score.json", "alias.json", "observed_refs.json", "responses.jsonl"]
    hashes = {name: hashlib.sha256((source / name).read_bytes()).hexdigest() for name in files}
    destination.mkdir(parents=True)
    provenance = {
        "kind": "submission-only-format-recovery", "source": str(source),
        "source_sha256": hashes, "original_stop_reason": original["stop_reason"],
        "source_plan": str(args.plan.resolve()),
        "source_plan_sha256": hashlib.sha256(args.plan.read_bytes()).hexdigest(),
        "retry_budget": args.retry_budget, "additional_observations": False,
        "comparable_to_unmodified_original": False,
        "student_source_sha256": hashlib.sha256((ROOT / "src/rca_lab/mcp/student.py").read_bytes()).hexdigest(),
    }
    write(destination / "recovery-provenance.json", provenance)
    config = StudentConfig(
        endpoint=plan["endpoint"], model=plan["model"], guided=plan["guided"],
        max_tokens=plan["max_output_tokens"], stop_on_output_limit=True,
        response_log=destination / "repair-responses.jsonl",
        temperature=plan["temperature"], top_p=plan["top_p"], top_k=plan["top_k"],
        seed=plan["seed"] + 1000 * original["run"], request_timeout=plan["request_timeout"],
        extra_body={"chat_template_kwargs": plan["chat_template_kwargs"]},
    )
    episode = run_forced_submit_repair(config, messages, aliases, retry_budget=args.retry_budget, observed_refs=refs)
    for name, digest in hashes.items():
        if hashlib.sha256((source / name).read_bytes()).hexdigest() != digest:
            raise RuntimeError(f"Original source changed during recovery: {name}")
    (destination / "trajectory.jsonl").write_text("".join(json.dumps(m, ensure_ascii=False) + "\n" for m in episode.messages))
    for name in ("alias.json", "observed_refs.json"):
        shutil.copy2(source / name, destination / name)
    if episode.result is not None:
        write(destination / "result.json", episode.result)
    # Inference is complete. Reference data is never supplied to the helper.
    from rca_lab.mcp.golden import load_contract
    from rca_lab.mcp.score import score_result
    expected = load_contract(ROOT / plan["contract"])[original["case_id"]]
    score = {**original, **score_result(
        expected, episode.result, turns=original["turns"] + episode.turns,
        tool_calls=original["tool_calls"] + episode.tool_calls,
        tool_errors=original["tool_errors"] + episode.tool_errors, observed_refs=refs,
    ), "stop_reason": episode.stop_reason,
        "wall_seconds": round(original["wall_seconds"] + episode.wall_seconds, 1),
        "repair_wall_seconds": round(episode.wall_seconds, 1),
        "repair_turns": episode.turns,
        "submission_recovery": True,
        "forced_submit_validation_retries": episode.forced_submit_validation_retries,
        "forced_submit_validation_retry_budget": args.retry_budget,
        "output_limit_hits": original["output_limit_hits"] + episode.output_limit_hits,
        "prompt_tokens_last": episode.prompt_tokens_last,
    }
    write(destination / "score.json", score)
    print(json.dumps(score, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
