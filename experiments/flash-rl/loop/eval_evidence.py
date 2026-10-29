"""Evidence quality of evaluation runs (2026-10-19, user: "근거도 측정").

For every run under eval/<tag>/<track>/shards with a score.json, run the same evidence judge the RL reward uses
(rl_reward.judge_trajectory_evidence, Claude Opus 5.5, cached next to the run) and summarise:
  joint          root + mechanism correct (mechanism judge, as in loop.evaluate)
  clean_joint    joint and no major unsupported/fabricated claim
  path / halluc  mean path_score / hallucination_score
  major_rate     share of runs with at least one major unsupported/fabricated claim
  right_target_extra_cause  runs that named every golden root but added another cause (root precision < 1)
  mean_turns / mean_output_tokens (submitted runs), joint_mean_* (joint runs only)  efficiency
  joint_mean_depth / deep_joint  causal depth of joint runs (rca_lab.mcp.causal_depth) / joint runs with >= 2 of 3 links
  decisive_recall / decisive_observed  share of the scenario's decisive evidence items the answer cited / the investigation reached
Writes eval/<tag>/<track>/evidence-summary.json and prints it. Judge failures are counted, never scored as zero.

  .venv/bin/python outputs/flash-rl-loop-20261015/eval_evidence.py eval/eval-u024/validation10 eval/milestone-u010/public_full
"""
from __future__ import annotations

import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from rca_lab.mcp.causal_depth import judge_causal_depth  # noqa: E402
from rca_lab.mcp.decisive_evidence import extract_decisive_items, judge_decisive_evidence  # noqa: E402
from rca_lab.mcp.numeric_grounding import ground_evidence_item  # noqa: E402
from rca_lab.mcp.rl_reward import build_trajectory_evidence_item, judge_cache_path, judge_trajectory_evidence  # noqa: E402

WORK = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "scripts"))
from mcp_judge_runs import case_dir  # noqa: E402
JUDGE = json.loads((WORK / "config.json").read_text())["judge"]


def load_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def output_tokens(run: Path) -> int | None:
    path = run / "responses.jsonl"
    if not path.exists():
        return None
    return sum(int((r.get("usage") or {}).get("completion_tokens") or 0) for r in load_jsonl(path))


def _mean(values):
    values = [v for v in values if v is not None]
    return round(sum(values) / len(values), 1) if values else None


def judge(run: Path) -> dict:
    score = json.loads((run / "score.json").read_text())
    mech = None   # the configured judge rubric's own verdict file (v3+ verdicts sit beside the v2 file)
    jp = judge_cache_path(run, JUDGE["provider"], model=JUDGE["model"], rubric=JUDGE.get("rubric", "v2"))
    if jp.exists():
        d = json.loads(jp.read_text())
        if d.get("model") == JUDGE["model"] and d.get("rubric", "v1") == JUDGE.get("rubric", "v2"):
            mech = d.get("score")
    joint = score.get("root_f1") == 1 and mech == 1
    row = {"case": score["case_id"], "run": run.name, "joint": joint, "submitted": score.get("stop_reason") == "submitted",
           # right target named but an extra cause added (prompt revision 2 targets this; 2026-10-19)
           "extra_cause": score.get("root_recall") == 1 and (score.get("root_precision") or 0) < 1,
           # efficiency (2026-10-19, user goal: accuracy, reasoning quality and efficient reasoning)
           "turns": score.get("turns"), "output_tokens": output_tokens(run)}
    if not row["submitted"] or not (run / "trajectory.jsonl").exists():
        return {**row, "status": "not_submitted"}
    messages, result = load_jsonl(run / "trajectory.jsonl"), json.loads((run / "result.json").read_text())
    # deterministic numeric grounding (numbers stated in the conclusion that appear in no observation)
    row["numeric_unverified"] = len(ground_evidence_item(build_trajectory_evidence_item(messages, result, score)).unverified)
    artifact, outcome = judge_trajectory_evidence(run, messages=messages, result=result, score=score,
                                                  model=JUDGE["model"], provider=JUDGE["provider"])
    if artifact is None:
        return {**row, "status": f"judge_failed:{outcome}"}
    v = artifact["verdict"]
    major = sum(1 for c in v.get("unsupported_or_fabricated_claims") or [] if c.get("severity") == "major")
    depth = None
    if joint:   # causal depth (grounded root-chain links / 3), judged on joint runs only, as in the RL reward
        record, _ = judge_causal_depth(run, messages=messages, result=result, score=score, model=JUDGE["model"], provider=JUDGE["provider"],
                                       rubric=JUDGE.get("depth_rubric", "rca-causal-depth-v1"))
        depth = record["verdict"]["depth"] if record else None
    # decisive-evidence recall (2026-10-21, user): did the answer rest on the evidence the scenario author calls decisive
    recall = observed = None
    meta = json.loads((case_dir(score["case_id"]) / "meta.json").read_text())
    decisive_text = (meta.get("scenario_metadata") or {}).get("distinguishing_evidence")
    if decisive_text:
        items = extract_decisive_items(decisive_text, WORK / "decisive-items" / f"{score['case_id']}.json", model=JUDGE["model"], provider=JUDGE["provider"])
        if items:
            rec, _ = judge_decisive_evidence(run, messages=messages, result=result, score=score, decisive_items=items,
                                             model=JUDGE["model"], provider=JUDGE["provider"])
            if rec:
                recall, observed = rec["verdict"]["recall"], rec["verdict"]["observed"]
    return {**row, "status": "judged", "path": v["path_score"], "halluc": v["hallucination_score"], "major": major, "depth": depth,
            "decisive_recall": recall, "decisive_observed": observed}


def summarise(track_dir: Path, workers: int = 6) -> dict:
    runs = sorted({p.parent for p in (track_dir / "shards").glob("**/run*/score.json")})
    with ThreadPoolExecutor(max_workers=workers) as pool:
        rows = list(pool.map(judge, runs))
    judged = [r for r in rows if r["status"] == "judged"]
    n = len(rows)
    out = {
        "track": str(track_dir.relative_to(WORK)), "runs": n, "judged": len(judged),
        "judge_failed": sum(r["status"].startswith("judge_failed") for r in rows),
        "joint": sum(r["joint"] for r in rows),
        "clean_joint": sum(1 for r in judged if r["joint"] and r["major"] == 0),
        "joint_with_major": sum(1 for r in judged if r["joint"] and r["major"] > 0),
        "path_mean": round(sum(r["path"] for r in judged) / len(judged), 3) if judged else None,
        "halluc_mean": round(sum(r["halluc"] for r in judged) / len(judged), 3) if judged else None,
        "major_rate": round(sum(r["major"] > 0 for r in judged) / len(judged), 3) if judged else None,
        "right_target_extra_cause": sum(1 for r in rows if r["extra_cause"]),
        "joint_mean_depth": (round(sum(d) / len(d), 3) if (d := [r["depth"] for r in judged if r.get("depth") is not None]) else None),
        "deep_joint": sum(1 for r in judged if (r.get("depth") or 0) >= 0.66),
        "decisive_recall": _mean([r.get("decisive_recall") for r in judged]),
        "decisive_observed": _mean([r.get("decisive_observed") for r in judged]),
        "joint_decisive_recall": _mean([r.get("decisive_recall") for r in judged if r["joint"]]),
        "mean_turns": _mean([r["turns"] for r in rows if r["submitted"]]),
        "mean_output_tokens": _mean([r["output_tokens"] for r in rows if r["submitted"]]),
        "joint_mean_turns": _mean([r["turns"] for r in rows if r["joint"]]),
        "joint_mean_output_tokens": _mean([r["output_tokens"] for r in rows if r["joint"]]),
        "numeric_unverified_rate": round(sum(r.get("numeric_unverified", 0) > 0 for r in judged) / len(judged), 3) if judged else None,
        "rows": rows,
    }
    (track_dir / "evidence-summary.json").write_text(json.dumps(out, ensure_ascii=False, indent=2))
    return out


if __name__ == "__main__":
    for arg in sys.argv[1:]:
        d = Path(arg) if Path(arg).is_absolute() else WORK / arg
        s = summarise(d)
        print(json.dumps({k: v for k, v in s.items() if k != "rows"}, ensure_ascii=False), flush=True)
