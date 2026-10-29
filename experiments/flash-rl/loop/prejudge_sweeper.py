"""Judge finished milestone runs while the rest of the milestone is still rolling (2026-10-23, user: "미리미리 채점하면안되나").

The loop judges a track only after all its runs finish. Every verdict is cached per run (mechanism verdicts under a per-run
lock, evidence/depth/decisive verdicts as files beside the run), so judging finished runs early only fills the caches the
loop's final pass reads. Stops once state.json records the milestone (default 39). Log: prejudge_sweeper.log.
"""
import argparse
import importlib.util
import json
import subprocess
import time
from pathlib import Path

WORK = Path(__file__).resolve().parent
ROOT = WORK.parents[1]
PY = str(ROOT / ".venv/bin/python")


def log(msg):
    with open(WORK / "prejudge_sweeper.log", "a") as f:
        f.write(f"[{time.strftime('%m-%d %H:%M:%S')}] {msg}\n")


def track_dirs(pattern):
    return sorted({p.parent for p in WORK.glob(f"eval/{pattern}/*/shards")})


def sweep(cfg, ev, pattern):
    for tdir in track_dirs(pattern):
        roots = sorted({p.parent.parent.parent for p in (tdir / "shards").glob("**/run*/score.json")})
        if not roots:
            continue
        subprocess.run([PY, "scripts/mcp_judge_runs.py", *map(str, roots), "--provider", cfg["provider"], "--model", cfg["model"],
                        "--rubric", cfg.get("rubric", "v2")], cwd=ROOT, capture_output=True, text=True)
        e = ev.summarise(tdir)   # evidence, depth and decisive verdicts (cached per run)
        log(f"{tdir.relative_to(WORK)}: {e['runs']} runs, judged {e['judged']}, judge_failed {e['judge_failed']}, joint {e['joint']}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--milestone", type=int, default=39)
    ap.add_argument("--pattern", default="milestone-*e3-mcp20261020-j3")
    ap.add_argument("--interval", type=int, default=600)
    args = ap.parse_args()
    cfg = json.loads((WORK / "config.json").read_text())["judge"]
    spec = importlib.util.spec_from_file_location("eval_evidence", WORK / "eval_evidence.py")
    ev = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ev)
    log(f"start: milestone {args.milestone}, pattern {args.pattern}")
    while True:
        state = json.loads((WORK / "state.json").read_text())
        if any(m.get("at") == args.milestone for m in state.get("milestone_evals", [])):
            log("milestone recorded; stopping")
            return
        try:
            sweep(cfg, ev, args.pattern)
        except Exception as exc:   # a half-written run dir; the next sweep retries
            log(f"sweep error: {str(exc)[:300]}")
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
