"""2026-10-28 fresh confirmation (Codex topics 3/6; user: "싹다 진행해줘 끝까지").

Waits for the context-policy paired control to finish (ctxpair run_pair.log "pair done"), takes its verdict (PROVISIONAL ADOPT ->
budget, otherwise rolling8), then serves each checkpoint on both GPUs in turn and runs the 6 development cases x 4 fresh runs
(same cases, run seeds, decoding, tool binary and limits for every checkpoint). judge_confirm.py applies the predeclared rule.
Checkpoints: u062 (start), sweep AdamW 3e-4, sweep AdamW 1e-4 (finalists, topic 6). Muon/1e-3 excluded.
"""
import importlib.util, json, subprocess, sys, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
WORK = HERE.parent.parent
PAIR = WORK / "diag" / "ctxpair-20261028"
SWEEP = "/home/work/models/sweep-20261028"
CHECKPOINTS = {   # tag[-4:] goes into the DB project prefix: must differ
    "conf-u062": {"version": "adapter-0f6f582d901317d0", "lora_dir": "/home/work/models/rca-adapters-vllm/flash-rl-loop-20261015/u062"},
    "conf-a3e4": {"version": "sweep-20261028-adamw-0.0003", "lora_dir": f"{SWEEP}/lora-arm-adamw-0.0003"},
    "conf-a1e4": {"version": "sweep-20261028-adamw-0.0001", "lora_dir": f"{SWEEP}/lora-arm-adamw-0.0001"},
}
PUBLIC = {"openrca2": "configs/teacher/openrca2-all-split.yaml", "rcaeval-tt4": "configs/teacher/rcaeval-tt4-split.yaml",
          "rcaeval-ob4": "configs/teacher/rcaeval-ob4-split.yaml"}
DEV = ["case-f19-s-v3-5daa9c97", "case-openrca2-o2ts-v3-83afcd5a", "case-rcaeval-ob4-v3-38c197c6", "case-rcaeval-ob4-v3-f9efedd9",
       "case-rcaeval-tt4-v3-e707b26b", "case-rcaeval-tt4-v3-f7cc5564"]


def load_loop():
    spec = importlib.util.spec_from_file_location("loop", WORK / "loop.py")
    loop = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loop)
    return loop


def say(msg):
    line = f"[{time.strftime('%m-%d %H:%M:%S')}] {msg}"
    with (HERE / "run_confirm.log").open("a") as f:
        f.write(line + "\n")
    print(line, flush=True)


def dev_track(loop):
    items = []
    for case in DEV:
        fam = next((k for k in PUBLIC if f"-{k}-" in case), None)
        if fam:
            root = "data/external/openrca2/captures" if fam == "openrca2" else "data/external/rcaeval/captures"
            items.append({"case": case, "cases_root": str(loop.ROOT / root), "contract": loop.public_contract(case), "split": str(loop.ROOT / PUBLIC[fam])})
        else:
            prep = json.loads(Path(loop.CONFIG["data"]["local_preparation"]).read_text())
            items.append({"case": case, "cases_root": "/data/eval-cases", "split": prep["split_source"],
                          "contract": prep.get("partition_contract", "configs/eval/train-family-v2-expanded-20261012.yaml")})
    return {"dev6": {"runs": 4, "items": items}}


def main():
    loop = load_loop()
    say("waiting for the context-policy paired control ('pair done')")
    while not ((PAIR / "run_pair.log").exists() and "pair done" in (PAIR / "run_pair.log").read_text()):
        time.sleep(120)
    verdict = (PAIR / "verdict.txt").read_text()
    policy = "budget" if "PROVISIONAL ADOPT" in verdict else "rolling8"
    loop.CONFIG["context_policy"] = policy
    say(f"paired control done -> context policy {policy}")
    tracks = dev_track(loop)
    (HERE / "tracks.json").write_text(json.dumps(tracks, indent=2))
    loop.keep_tunnel()
    for tag, ckpt in CHECKPOINTS.items():
        if (WORK / "eval" / tag / "result.json").exists():
            say(f"{tag}: already done, skipped")
            continue
        model = loop.start_servers(ckpt)
        assert model == loop.CONFIG["lora_name"], model
        say(f"{tag}: serving {ckpt['version']}")
        res = loop.evaluate(tag, model, {"policy": ckpt}, tracks=tracks, policy_version=ckpt["version"])
        res["context_policy"] = policy
        (WORK / "eval" / tag / "result.json").write_text(json.dumps(res, indent=2))
        say(f"{tag} done: " + json.dumps({t: {k: v.get(k) for k in ("total", "root_hit", "root_plus_mechanism", "failed_cases")} for t, v in res["tracks"].items()}))
    loop.stop_servers()
    r = subprocess.run([sys.executable, str(HERE / "judge_confirm.py")], cwd=HERE, capture_output=True, text=True)
    (HERE / "verdict.txt").write_text(r.stdout + r.stderr)
    say("verdict written:\n" + r.stdout[-3000:] + r.stderr[-1000:])
    say("confirm done")


if __name__ == "__main__":
    main()
