"""Agreement between the deterministic numeric grounding check and the Claude evidence judge (2026-10-19).

Runs rca_lab.mcp.numeric_grounding on every judged rollout episode (the evidence artifact embeds the judge item: the
submitted conclusion and every observation before the submit) and compares with the judge's verdict:
  episode level  numeric check flags >= 1 unverified number  vs  judge lists >= 1 major claim that contains a number
  claim level    recall: judge's numeric major claims whose number the check also flags as unverified
                 precision: check's unverified numbers that occur in some judge claim (major or minor)
Writes numeric-vs-judge.json (stats + disagreement samples) next to this script.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

WORK = Path(__file__).resolve().parent
sys.path.insert(0, str(WORK.parents[1] / "src"))
from rca_lab.mcp.numeric_grounding import extract_claims, ground_evidence_item  # noqa: E402


def same(a, b) -> bool:
    if a.unit != b.unit:
        return False
    return abs(a.value - b.value) <= max(a.tolerance, b.tolerance) + 1e-9


def main() -> None:
    eps = []
    for g in sorted(WORK.glob("u0[0-9][0-9]/rollouts/*/group.evidence-v2.json")):
        try:
            data = json.loads(g.read_text())
        except json.JSONDecodeError:
            continue
        for e in data["episodes"]:
            art = e.get("evidence_artifact") or {}
            if art.get("verdict") and art.get("item"):
                eps.append((g.parent.name, e, art))
    tp = fp = fn = tn = 0
    judge_numeric_major = caught = 0
    ours_total = ours_in_judge = 0
    samples = {"judge_only": [], "ours_only": [], "both": []}
    for case, e, art in eps:
        res = ground_evidence_item(art["item"])
        ours = res.unverified
        claims = art["verdict"].get("unsupported_or_fabricated_claims") or []
        major_num = [(c, extract_claims(c.get("claim", ""))) for c in claims if c.get("severity") == "major"]
        major_num = [(c, ns) for c, ns in major_num if ns]
        judge_flag, our_flag = bool(major_num), bool(ours)
        tp += judge_flag and our_flag; fp += our_flag and not judge_flag
        fn += judge_flag and not our_flag; tn += not judge_flag and not our_flag
        for c, ns in major_num:
            judge_numeric_major += 1
            hit = any(same(n, o) for n in ns for o in ours)
            caught += hit
            if not hit and len(samples["judge_only"]) < 12:
                samples["judge_only"].append({"case": case, "claim": c.get("claim", "")[:220], "why": c.get("explanation", "")[:220]})
        judge_nums = [n for c in claims for n in extract_claims(c.get("claim", ""))]
        for o in ours:
            ours_total += 1
            if any(same(o, n) for n in judge_nums):
                ours_in_judge += 1
                if len(samples["both"]) < 6:
                    samples["both"].append({"case": case, "number": o.text, "context": o.context})
            elif len(samples["ours_only"]) < 15:
                samples["ours_only"].append({"case": case, "number": o.text, "context": o.context, "joint": e["joint_success"]})
    stats = {
        "episodes": len(eps),
        "episode_level": {"both_flag": tp, "only_numeric_check": fp, "only_judge": fn, "neither": tn},
        "judge_numeric_major_claims": judge_numeric_major,
        "recall_of_judge_numeric_major": round(caught / judge_numeric_major, 3) if judge_numeric_major else None,
        "numeric_unverified": ours_total,
        "share_of_numeric_unverified_also_in_judge_claims": round(ours_in_judge / ours_total, 3) if ours_total else None,
    }
    (WORK / "numeric-vs-judge.json").write_text(json.dumps({"stats": stats, "samples": samples}, ensure_ascii=False, indent=2))
    print(json.dumps(stats, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
