"""Predeclared decision for the 2026-10-28 context-policy paired control (diag/context-policy-20261028.md, Codex topic 4).

Joint success = root_f1 == 1 and mechanism verdict == 1 under the configured judge. A run that is missing or unscored counts
as a failure (errors/timeouts reported separately). diff = budget - rolling8, paired by (track, case). Bootstrap: within each
track resample whole cases (all paired runs kept), recompute the episode-weighted total; 90% percentile interval.
  provisional adopt: total >= -2 and no track <= -3 and lower bound > -6
  hold:              total <= -5 or any track <= -4
  otherwise:         inconclusive -> keep rolling8 (budget stays a measured ablation; no selective reruns)
"""
import importlib.util, json, random, statistics as st
from pathlib import Path

HERE = Path(__file__).resolve().parent
WORK = HERE.parent.parent
spec = importlib.util.spec_from_file_location("loop", WORK / "loop.py")
loop = importlib.util.module_from_spec(spec); spec.loader.exec_module(loop)
TAGS = {"rolling8": "ctx-u062-rol8", "budget": "ctx-u062-bdgt"}


def arm_table(tag):
    table, extra = {}, {"errors": 0, "unjudged": 0, "turns": [], "prompt_tokens_last": [], "seconds": []}
    for track, tspec in loop.CONFIG["eval"]["tracks"].items():
        for item in tspec["items"]:
            runs = sorted((WORK / "eval" / tag / track / "shards").glob(f"*/{item['case']}/run*/score.json"))
            joints = []
            for sp in runs[: tspec["runs"]]:
                s = json.loads(sp.read_text())
                m = loop.mechanism_score(sp.parent)
                extra["unjudged"] += m is None and s.get("root_f1") == 1   # judge runs only on root hits
                joints.append(int(s.get("root_f1") == 1 and m == 1))
                for k, out in (("turns", "turns"), ("prompt_tokens_last", "prompt_tokens_last"), ("wall_seconds", "seconds")):
                    if isinstance(s.get(k), (int, float)):
                        extra[out].append(s[k])
            extra["errors"] += tspec["runs"] - len(joints)
            table[(track, item["case"])] = joints + [0] * (tspec["runs"] - len(joints))
    return table, extra


def main():
    a, ea = arm_table(TAGS["rolling8"]); b, eb = arm_table(TAGS["budget"])
    keys = sorted(a)
    diff = {k: sum(b[k]) - sum(a[k]) for k in keys}
    tracks = sorted({t for t, _ in keys})
    per_track = {t: sum(d for (tt, _), d in diff.items() if tt == t) for t in tracks}
    total = sum(diff.values())
    rng = random.Random(20261028); boots = []
    by_track = {t: [diff[k] for k in keys if k[0] == t] for t in tracks}
    for _ in range(10000):
        boots.append(sum(sum(rng.choice(v) for _ in v) for v in by_track.values()))
    boots.sort(); lo, hi = boots[int(0.05 * len(boots))], boots[int(0.95 * len(boots)) - 1]
    if total >= -2 and all(v > -3 for v in per_track.values()) and lo > -6:
        verdict = "PROVISIONAL ADOPT budget"
    elif total <= -5 or any(v <= -4 for v in per_track.values()):
        verdict = "HOLD (keep rolling8, investigate)"
    else:
        verdict = "INCONCLUSIVE -> keep rolling8; budget = measured ablation"
    print(f"joint rolling8 {sum(map(sum, a.values()))}/{sum(map(len, a.values()))}  budget {sum(map(sum, b.values()))}/{sum(map(len, b.values()))}")
    for t in tracks:
        print(f"  {t:13s} rolling8 {sum(sum(a[k]) for k in keys if k[0] == t)}  budget {sum(sum(b[k]) for k in keys if k[0] == t)}  diff {per_track[t]:+d}")
    print(f"total diff {total:+d}, case-bootstrap 90% [{lo:+d}, {hi:+d}] over {len(keys)} cases")
    print("cases that differ:", {f"{t}/{c}": d for (t, c), d in diff.items() if d})
    for name, e in (("rolling8", ea), ("budget", eb)):
        print(f"{name}: missing/error runs {e['errors']}, unjudged {e['unjudged']}, turns mean {st.mean(e['turns']) if e['turns'] else None}, "
              f"prompt_tokens_last p50 {st.median(e['prompt_tokens_last']) if e['prompt_tokens_last'] else None} max {max(e['prompt_tokens_last'], default=None)}, "
              f"episode seconds mean {st.mean(e['seconds']) if e['seconds'] else None}")
    print("VERDICT:", verdict)


if __name__ == "__main__":
    main()
