"""How far has the served policy moved from the base model? (2026-10-24 diagnosis, user request)

Re-scores the completion tokens of recent rollout turns under the base model and under the policy LoRA on the same vLLM
server (prompt_logprobs), and compares both to the sampler's stored old_logprobs. Reports per-token mean log-ratio
(= a sample estimate of KL(policy || base) on policy-sampled tokens), mean |log-ratio|, and the base-vs-base repeat noise
floor. Read-only: needs the loop paused with servers up.
"""
import argparse
import json
import random
import statistics as st
import urllib.request
from pathlib import Path

WORK = Path(__file__).resolve().parents[1]


def score(port, model, prompt_ids, completion_ids):
    body = {"model": model, "prompt": prompt_ids + completion_ids, "max_tokens": 1, "temperature": 0, "prompt_logprobs": 0}
    req = urllib.request.Request(f"http://127.0.0.1:{port}/v1/completions", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=1800) as r:
        out = json.load(r)
    pl = out["choices"][0]["prompt_logprobs"][len(prompt_ids):]
    return [next(iter(d.values()))["logprob"] if len(d) == 1 else d[str(t)]["logprob"] for d, t in zip(pl, completion_ids)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--updates", default="u048,u049,u050")
    ap.add_argument("--turns-per-episode", type=int, default=2)
    ap.add_argument("--max-prompt", type=int, default=24000)
    ap.add_argument("--port", type=int, default=18241)
    ap.add_argument("--base", default="qwen-flash-next-int4")
    ap.add_argument("--policy", default="policy")
    ap.add_argument("--out", default=str(WORK / "diag" / "logprob_shift.json"))
    args = ap.parse_args()
    rng = random.Random(20261024)
    picks = []
    for u in args.updates.split(","):
        for g in sorted((WORK / u / "rollouts").glob("*/group*.json")):
            d = json.loads(g.read_text())
            for e in d["episodes"][:2]:
                turns = [t for t in e.get("turns", []) if len(t["prompt_token_ids"]) <= args.max_prompt and t["completion_token_ids"]]
                for t in rng.sample(turns, min(args.turns_per_episode, len(turns))):
                    picks.append((u, d["case_id"], e["episode_id"], t))
    rows = []
    for u, case, ep, t in picks:
        p, c = t["prompt_token_ids"], t["completion_token_ids"]
        lb = score(args.port, args.base, p, c)
        lp = score(args.port, args.policy, p, c)
        lb2 = score(args.port, args.base, p, c) if len(rows) < 6 else None   # repeat noise floor on a few turns
        old = t["old_logprobs"]
        r = {"update": u, "case": case, "episode": ep, "prompt_tokens": len(p), "tokens": len(c),
             "policy_minus_base": st.mean(a - b for a, b in zip(lp, lb)),
             "abs_policy_minus_base": st.mean(abs(a - b) for a, b in zip(lp, lb)),
             "policy_minus_sampler": st.mean(a - b for a, b in zip(lp, old)),
             "abs_policy_minus_sampler": st.mean(abs(a - b) for a, b in zip(lp, old)),
             "base_minus_sampler": st.mean(a - b for a, b in zip(lb, old))}
        if lb2:
            r["abs_base_repeat"] = st.mean(abs(a - b) for a, b in zip(lb, lb2))
        rows.append(r)
        print(json.dumps(r), flush=True)
    tw = sum(r["tokens"] for r in rows)
    agg = {k: sum(r[k] * r["tokens"] for r in rows) / tw for k in
           ("policy_minus_base", "abs_policy_minus_base", "policy_minus_sampler", "abs_policy_minus_sampler", "base_minus_sampler")}
    rep = [r["abs_base_repeat"] for r in rows if "abs_base_repeat" in r]
    agg["abs_base_repeat_mean"] = st.mean(rep) if rep else None
    agg["turns"], agg["tokens"] = len(rows), tw
    Path(args.out).write_text(json.dumps({"aggregate": agg, "rows": rows}, indent=2))
    print("AGG", json.dumps(agg))


if __name__ == "__main__":
    main()
