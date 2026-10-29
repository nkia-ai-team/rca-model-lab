"""Held-out transfer score for the 2026-10-28 lr/optimizer sweep.

Each sweep arm is one update from u063 on the same 4 training groups. This re-scores sampled turns of held-out groups (cases the
arms never trained on) under every served adapter on the same vLLM servers (prompt_logprobs) and reports, per arm, how much the
update moved log-probability toward the better episodes of each held-out group:
    transfer = mean over groups of mean_i A_i * d_i,   A_i = (r_i - mean r) / (std r + 1e-6),   d_i = mean token (lp_arm - lp_start)
plus the joint split (d on joint-success episodes minus d on failed ones, mixed groups only; PRIMARY screen per the 10-28
Codex debate, reward transfer secondary), and the drift on in-distribution
probe groups (all-success polestar/public cases). 'start-repeat' (the start adapter scored a second time) gives the noise floor.
Runs on KT next to the servers; stdlib only.
"""
import argparse
import json
import random
import statistics as st
import threading
import urllib.request
from pathlib import Path


def score(port, model, prompt_ids, completion_ids):
    body = {"model": model, "prompt": prompt_ids + completion_ids, "max_tokens": 1, "temperature": 0, "prompt_logprobs": 0}
    req = urllib.request.Request(f"http://127.0.0.1:{port}/v1/completions", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=3600) as r:
        out = json.load(r)
    pl = out["choices"][0]["prompt_logprobs"][len(prompt_ids):]
    return [next(iter(d.values()))["logprob"] if len(d) == 1 else d[str(t)]["logprob"] for d, t in zip(pl, completion_ids)]


def picks_for(paths, turns_per_episode, max_prompt, rng):
    out = []
    for path in paths:
        d = json.loads(Path(path).read_text())
        for e in d["episodes"]:
            turns = [t for t in e["turns"] if len(t["prompt_token_ids"]) <= max_prompt and t["completion_token_ids"]]
            # always the last scorable turn (final diagnosis), plus random earlier tool-decision turns; same picks for every arm
            chosen = turns[-1:] + rng.sample(turns[:-1], min(turns_per_episode - 1, len(turns) - 1)) if turns else []
            for t in chosen:
                out.append(dict(case=d["case_id"], episode=e["episode_id"], reward=e["reward"], joint=bool(e["joint_success"]),
                                prompt=t["prompt_token_ids"], completion=t["completion_token_ids"]))
    return out


def run_all(picks, models, ports):
    """Score every pick under every model; picks are split across servers, models run in sequence per pick."""
    results = [None] * len(picks)
    def worker(port, idx):
        for i in idx:
            p = picks[i]
            results[i] = {m: score(port, m, p["prompt"], p["completion"]) for m in models}
            print(f"scored {i + 1}/{len(picks)} on {port}", flush=True)
    threads = [threading.Thread(target=worker, args=(port, list(range(k, len(picks), len(ports))))) for k, port in enumerate(ports)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


def summarize(picks, results, models, ref):
    """Per model: per-episode token-weighted mean (lp_model - lp_ref), then the transfer / joint-split / drift aggregates."""
    per = {}
    for m in models:
        eps = {}
        for p, r in zip(picks, results):
            diff = [a - b for a, b in zip(r[m], r[ref])]
            k = (p["case"], p["episode"])
            acc = eps.setdefault(k, dict(case=p["case"], reward=p["reward"], joint=p["joint"], s=0., n=0, abs=0.))
            acc["s"] += sum(diff); acc["n"] += len(diff); acc["abs"] += sum(abs(x) for x in diff)
        groups = {}
        for e in eps.values():
            e["d"] = e["s"] / e["n"]
            groups.setdefault(e["case"], []).append(e)
        transfer, split, gtab = [], [], {}
        for case, es in groups.items():
            rs = [e["reward"] for e in es]
            mu, sd = st.mean(rs), (st.pstdev(rs) if len(rs) > 1 else 0.)
            t = st.mean((e["reward"] - mu) / (sd + 1e-6) * e["d"] for e in es)
            transfer.append(t)
            row = dict(transfer=round(t, 5))
            win, lose = [e["d"] for e in es if e["joint"]], [e["d"] for e in es if not e["joint"]]
            if win and lose:
                row["joint_split"] = round(st.mean(win) - st.mean(lose), 5)
                split.append(row["joint_split"])
            gtab[case] = row
        tok = sum(e["n"] for e in eps.values())
        absd = sorted(abs(e["d"]) for e in eps.values())
        per[m] = dict(transfer=st.mean(transfer), transfer_positive_groups=sum(t > 0 for t in transfer), groups=len(transfer),
                      joint_split=st.mean(split) if split else None, mean_shift=sum(e["s"] for e in eps.values()) / tok,
                      mean_abs_shift=sum(e["abs"] for e in eps.values()) / tok, tokens=tok,
                      episode_abs_shift_p90=absd[int(0.9 * (len(absd) - 1))], episode_abs_shift_max=absd[-1], by_group=gtab)
    return per


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--heldout", nargs="+", required=True)
    ap.add_argument("--probe", nargs="*", default=[])
    ap.add_argument("--models", nargs="+", required=True, help="served adapter names; the first is the start adapter")
    ap.add_argument("--ports", default="18241,18242")
    ap.add_argument("--turns-per-episode", type=int, default=4)
    ap.add_argument("--max-prompt", type=int, default=90000)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    ports = [int(x) for x in args.ports.split(",")]
    ref = args.models[0]
    models = [ref, "start-repeat"] + args.models[1:]
    rng = random.Random(20261028)
    out = {}
    for name, paths in (("heldout", args.heldout), ("probe", args.probe)):
        if not paths:
            continue
        picks = picks_for(paths, args.turns_per_episode, args.max_prompt, rng)
        # score ref twice (second pass = repeat noise floor), then each arm once
        results = run_all(picks, [ref] + args.models[1:], ports)
        repeat = run_all(picks, [ref], ports)
        for r, rr in zip(results, repeat):
            r["start-repeat"] = rr[ref]
        out[name] = summarize(picks, results, models[1:], ref)
        print(name, json.dumps({m: {k: v for k, v in s.items() if k != "by_group"} for m, s in out[name].items()}), flush=True)
    Path(args.out).write_text(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
