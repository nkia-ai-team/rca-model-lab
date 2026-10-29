"""W&B tracking for the Flash RL loop (2026-10-26, user: "언젠가부터 w&b 안쓰는거같은데").

The loop never logged to W&B (loop.py has no tracking; the KT trainer venv has no wandb), against the repo's mandatory
tracking contract (src/rca_lab/train/tracking.py). This sidecar reads state.json, back-fills every past update/eval/milestone
into one resumable online run and then follows new entries. It never writes loop files.

  uv run --no-project --with wandb python wandb_sync.py [--once]

Charts use the custom x-axis "update" (loop update index k). Effective learning rate: AdamW.load_state_dict restored the
first update's 5e-5 for every update through u63 (bug fixed 2026-10-26, diag/README-20261024.md §5); from u64 the release
records effective_learning_rate.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

WORK = Path(__file__).resolve().parent
sys.path.insert(0, str(WORK.parents[1] / "src"))
from rca_lab.train.tracking import require_wandb, wandb_run_name  # noqa: E402

ENTITY, PROJECT, RUN_ID = "nkia-ai", "rca-flash-rl", "flash-rl-loop-20261015"
LEDGER = WORK / "wandb_sync.ledger.json"   # what has been logged already
LR_BUG_LAST_UPDATE, LR_BUG_VALUE = 63, 5e-5


def source(case):
    if "openrca2" in case:
        return "openrca2"
    m = re.search(r"rcaeval-(\w+)-", case)
    return f"rcaeval_{m.group(1)}" if m else "polestar"


def update_row(h, updated_index):
    rel = h.get("release") or {}
    rewards = h.get("rewards") or {}
    row = {"update": h["k"], "loop/updated": int(h.get("result") == "updated"), "loop/updated_count": updated_index}
    flat = [x for r in rewards.values() for x in r]
    if flat:
        row["rollout/mean_reward"] = sum(flat) / len(flat)
        row["rollout/success_rate_ge_0.7"] = sum(x >= .7 for x in flat) / len(flat)
    by = {}
    for case, rs in rewards.items():
        by.setdefault(source(case), []).extend(rs)
    for s, rs in by.items():
        row[f"rollout/{s}/mean_reward"] = sum(rs) / len(rs)
    if h.get("result") == "updated":
        row["train/optimizer_steps"] = rel.get("optimizer_steps")
        row["train/steps_this_update"] = rel.get("steps_this_update", 1)
        row["train/preclip_grad_norm"] = rel.get("preclip_gradient_norm")
        row["train/loss_total"] = rel.get("loss_total")
        row["train/groups"] = len(h.get("groups") or [])
        row["train/configured_lr"] = h.get("learning_rate")
        row["train/effective_lr"] = rel.get("effective_learning_rate") or (LR_BUG_VALUE if h["k"] <= LR_BUG_LAST_UPDATE else h.get("learning_rate"))
    return {k: v for k, v in row.items() if v is not None}


def track_metrics(prefix, tracks):
    out = {}
    for t, v in tracks.items():
        n = v.get("total") or 0
        if not n:
            continue
        out[f"{prefix}/{t}/joint_rate"] = v["root_plus_mechanism"] / n
        out[f"{prefix}/{t}/root_rate"] = v["root_hit"] / n
        out[f"{prefix}/{t}/total"] = n
        ev = v.get("evidence") or {}
        if ev.get("clean_joint") is not None:
            out[f"{prefix}/{t}/clean_joint_rate"] = ev["clean_joint"] / n
        for key in ("major_rate", "path_mean", "halluc_mean", "numeric_unverified_rate"):
            if ev.get(key) is not None:
                out[f"{prefix}/{t}/{key}"] = ev[key]
    return out


def epoch_name(e):
    return f"e{e.get('epoch') or '1'}".replace("-", "_")


def sync(run, wandb, ledger):
    state = json.loads((WORK / "state.json").read_text())
    done = set(ledger.get("logged", []))
    count, k_of_count = 0, {}
    for h in state["history"]:
        if h.get("result") == "updated":
            count += 1
            k_of_count[count] = h["k"]
        key = f"update:{h['k']}:{h.get('result')}"
        if key not in done:
            wandb.log(update_row(h, count)); done.add(key)
    for e in state.get("evals", []):
        m = re.search(r"u(\d+)", e.get("tag", ""))
        key = f"eval:{e.get('tag')}:{e.get('epoch')}"
        if m and key not in done:
            wandb.log({"update": int(m.group(1)), **track_metrics(f"eval_{epoch_name(e)}", e["tracks"])}); done.add(key)
    for m in state.get("milestone_evals", []):
        key = f"milestone:{m['at']}:{m.get('epoch')}"
        if key not in done and m["at"] in k_of_count:
            wandb.log({"update": k_of_count[m["at"]], **track_metrics(f"milestone_{epoch_name(m)}", m["tracks"])}); done.add(key)
    refs = {**({"1": state["milestone_reference"]} if state.get("milestone_reference") else {}), **(state.get("milestone_reference_by_rev") or {})}
    for ep, ref in refs.items():
        key = f"milestone_ref:{ep}:{json.dumps({t: v.get('total') for t, v in ref['tracks'].items()}, sort_keys=True)}"
        if key not in done:
            run.summary.update(track_metrics(f"milestone_reference_e{ep}".replace("-", "_"), ref["tracks"])); done.add(key)
    for r in state.get("rollbacks", []):
        key = f"rollback:{r['k']}"
        if key not in done:
            wandb.log({"update": r["k"], "loop/rollback": 1}); done.add(key)
    ledger["logged"] = sorted(done)
    LEDGER.write_text(json.dumps(ledger, indent=1))
    return len(done)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--interval", type=int, default=600)
    args = ap.parse_args()
    wandb = require_wandb()
    config = json.loads((WORK / "config.json").read_text())
    ledger = json.loads(LEDGER.read_text()) if LEDGER.exists() else {}
    # canonical tracking name (tracking.wandb_run_name); a fixed id so restarts resume the same run
    name = ledger.get("run_name") or wandb_run_name(method="grpo-loop", model_name=config["checkpoint"], version=RUN_ID)
    run = wandb.init(entity=ENTITY, project=PROJECT, id=RUN_ID, name=name, resume="allow",
                     config={"loop_config": config, "tracking/run_name": name, "tracking/method": "grpo-loop",
                             "tracking/model": Path(config["checkpoint"]).name, "tracking/version": RUN_ID, "tracking/timezone": "UTC",
                             "note_lr_bug": "effective lr 5e-5 through u63 (load_state_dict), configured lr applies from u64"})
    if run is None:
        raise RuntimeError("wandb.init returned no run; refusing untracked loop")
    ledger["run_name"] = name
    wandb.define_metric("update")
    wandb.define_metric("*", step_metric="update")
    while True:
        n = sync(run, wandb, ledger)
        print(time.strftime("%m-%d %H:%M:%S"), "logged entries", n, flush=True)
        if args.once:
            break
        time.sleep(args.interval)
    run.finish()


if __name__ == "__main__":
    main()
