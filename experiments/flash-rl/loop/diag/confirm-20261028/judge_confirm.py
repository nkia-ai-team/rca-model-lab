"""Predeclared rule for the 2026-10-28 confirmation (Codex topic 3, round 3; topic 6).

Joint = root_f1 == 1 and mechanism verdict == 1; a missing/unscored run is a failure. Per finalist vs u062 on 6 dev cases x 4:
  provisional progress: total gain >= +3/24 AND net gain on >= 2 of 6 cases (operational threshold, not statistical proof)
  both pass, finalist difference unresolved (|diff| < 3) -> lower LR (1e-4); else the higher one
  neither passes -> keep u062 as start; lower-LR (1e-4) continuation only as budgeted exploration (+3/+6 checkpoints)
Per-case differences are printed; a finalist with a case loss of >= 3/4 is flagged for review (possible regression veto).
"""
import importlib.util, json
from pathlib import Path

HERE = Path(__file__).resolve().parent
WORK = HERE.parent.parent
spec = importlib.util.spec_from_file_location("loop", WORK / "loop.py")
loop = importlib.util.module_from_spec(spec); spec.loader.exec_module(loop)
TRACKS = json.loads((HERE / "tracks.json").read_text())
BASE, FIN = "conf-u062", {"conf-a3e4": "adamw 3e-4", "conf-a1e4": "adamw 1e-4"}


def table(tag):
    out, extra = {}, {"turns": [], "seconds": []}
    for track, tspec in TRACKS.items():
        for item in tspec["items"]:
            runs = sorted((WORK / "eval" / tag / track / "shards").glob(f"*/{item['case']}/run*/score.json"))[: tspec["runs"]]
            js = []
            for sp in runs:
                s = json.loads(sp.read_text())
                js.append(int(s.get("root_f1") == 1 and loop.mechanism_score(sp.parent) == 1))
                for k, o in (("turns", "turns"), ("wall_seconds", "seconds")):
                    if isinstance(s.get(k), (int, float)):
                        extra[o].append(s[k])
            out[item["case"]] = sum(js)
    return out, extra


def main():
    base, eb = table(BASE)
    n = sum(t["runs"] * len(t["items"]) for t in TRACKS.values())
    print(f"u062 joint {sum(base.values())}/{n}")
    passed, tot = {}, {}
    for tag, name in FIN.items():
        t, e = table(tag)
        diff = {c: t[c] - base[c] for c in base}
        tot[tag] = sum(t.values())
        gain, pos = sum(diff.values()), sum(d > 0 for d in diff.values())
        ok = gain >= 3 and pos >= 2
        passed[tag] = ok
        flag = [c for c, d in diff.items() if d <= -3]
        mean = lambda v: round(sum(v) / len(v), 1) if v else None
        print(f"{name}: joint {tot[tag]}/{n}  gain {gain:+d}  cases up {pos}/6  -> {'PROVISIONAL PROGRESS' if ok else 'no progress'}"
              f"  turns {mean(e['turns'])} (u062 {mean(eb['turns'])})" + (f"  REVIEW case losses {flag}" if flag else ""))
        print("   per case " + ", ".join(f"{c[5:20]} {base[c]}->{t[c]}" for c in base))
    a, b = "conf-a3e4", "conf-a1e4"
    if passed[a] and passed[b]:
        pick = a if tot[a] - tot[b] >= 3 else b
    elif passed[a] or passed[b]:
        pick = a if passed[a] else b
    else:
        pick = None
    if pick:
        print(f"DECISION: resume from {FIN[pick]} checkpoint ({pick})")
    else:
        print("DECISION: no demonstrated improvement -> keep u062; continue with adamw 1e-4 as budgeted exploration (+3/+6 gates)")


if __name__ == "__main__":
    main()
