"""Gate 1 of the hinted-data path: grade hint-ladder runs and write review.json.

  uv run python scripts/mcp_ladder_accept.py --root outputs/mcp-eval/ladder-rl1-i3 --root outputs/mcp-eval/ladder-rl1-i3-mech

A hinted run is accepted when it was submitted, names every root (root_f1 = 1), cites only observed
refs (ref_grounding = 1), the mechanism judge gives it 1 (v2, cached in mechanism_judge.json), and
its final answer does not talk about the guidance. Train captures only. Next: mcp_rationalize.py
--student-root <root> --student-require-review (see rca_lab.mcp.hinted_data).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from rca_lab.mcp.hinted_data import ladder_verdict
from rca_lab.mcp.rl_reward import conclusion_text, judge_cache_valid, judge_mechanism, reference_cause, reference_version


def mechanism_grade(run_dir: Path, case_id: str, result: dict | None, *, model: str, cases_root: Path) -> float | None:
    """The v2 grade from the shared cache (same model), judging once when it is missing."""
    path = run_dir / "mechanism_judge.json"
    if path.exists():
        cached = json.loads(path.read_text(encoding="utf-8"))
        if judge_cache_valid(cached, case_id, model=model, rubric="v2"):
            return cached.get("score")
    if not result:
        return None
    value, detail = judge_mechanism(reference_cause(cases_root / case_id), conclusion_text(result), model=model, rubric="v2")
    if value is not None:
        path.write_text(json.dumps({"score": value, "detail": detail, "model": model, "rubric": "v2", "reference": reference_version(case_id)}, ensure_ascii=False, indent=1), encoding="utf-8")
    return value


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", action="append", required=True, type=Path)
    parser.add_argument("--split", default="configs/teacher/v45-family-split-val.yaml")
    parser.add_argument("--judge-model", default="claude-opus-5-5")
    parser.add_argument("--cases-root", type=Path, default=Path("/data/eval-cases"))
    args = parser.parse_args()
    train = set(yaml.safe_load(Path(args.split).read_text(encoding="utf-8"))["train"])
    counts: Counter[str] = Counter()
    for root in args.root:
        for score_path in sorted(root.glob("case-*/run*/score.json")):
            score = json.loads(score_path.read_text(encoding="utf-8"))
            if not score.get("hinted"):
                continue
            run_dir = score_path.parent
            case_id = score["case_id"]
            if case_id not in train:
                raise SystemExit(f"non-train capture in a hint-ladder root (hints must never touch validation or sealed): {case_id}")
            result_path = run_dir / "result.json"
            result = json.loads(result_path.read_text(encoding="utf-8")) if result_path.exists() else None
            mechanism = None
            if float(score.get("root_f1") or 0.0) == 1.0 and score.get("stop_reason") == "submitted":
                mechanism = mechanism_grade(run_dir, case_id, result, model=args.judge_model, cases_root=args.cases_root)
            accepted, note = ladder_verdict(score, result, mechanism)
            review = {"accepted": accepted, "note": note, "root_f1": score.get("root_f1"), "mechanism": mechanism, "hint_rung": score.get("hint_rung"), "reviewed": "auto-ladder"}
            (run_dir / "review.json").write_text(json.dumps(review, ensure_ascii=False, indent=1), encoding="utf-8")
            counts["accepted" if accepted else "rejected"] += 1
            if accepted:
                print(f"  accepted {root.name}/{case_id}/{run_dir.name} (rung {score.get('hint_rung')})", flush=True)
    print(f"[ladder-accept] accepted={counts['accepted']} rejected={counts['rejected']}", flush=True)


if __name__ == "__main__":
    main()
