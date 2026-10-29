"""2026-10-28 paired control (user: "스윕·held-out 채점 끝나면 u062 rolling8 vs budget 대조 시험(116판) 바로 돌려").

Parent: waits for the sweep's held-out scoring to finish on KT ("heldout done"), serves u062 on both GPUs, then runs the two
arms concurrently as child processes (same cases, seeds, decoding, tool binary, limits; each case lands on the same server
in both arms), each with half of the episode slots. Children: loop.evaluate with CONFIG["context_policy"] set per arm.
Results land in ../../eval/<tag>/; judge() in judge_pair.py applies the predeclared rule (context-policy-20261028.md).
"""
import argparse, importlib.util, json, subprocess, sys, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
WORK = HERE.parent.parent
U062 = {"version": "adapter-0f6f582d901317d0", "lora_dir": "/home/work/models/rca-adapters-vllm/flash-rl-loop-20261015/u062"}
ARMS = {"rolling8": "ctx-u062-rol8", "budget": "ctx-u062-bdgt"}   # tag[-4:] is in the DB project prefix: must differ
SWEEP = "/home/work/models/sweep-20261028"


def load_loop():
    spec = importlib.util.spec_from_file_location("loop", WORK / "loop.py")
    loop = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loop)
    return loop


def say(msg):
    line = f"[{time.strftime('%m-%d %H:%M:%S')}] {msg}"
    with (HERE / "run_pair.log").open("a") as f:
        f.write(line + "\n")
    print(line, flush=True)


def child(arm):
    loop = load_loop()
    loop.CONFIG["context_policy"] = arm
    loop.CONFIG["eval"]["episode_slots"] = loop.CONFIG["eval"].get("episode_slots", 12) // 2
    tag = ARMS[arm]
    res = loop.evaluate(tag, loop.CONFIG["lora_name"], {"policy": U062}, policy_version=U062["version"])
    res["context_policy"] = arm; res["epoch"] = loop.epoch_key()
    (WORK / "eval" / tag / "result.json").write_text(json.dumps(res, indent=2))
    say(f"arm {arm} done: " + json.dumps({t: {k: v.get(k) for k in ("total", "root_hit", "root_plus_mechanism", "failed_cases")} for t, v in res["tracks"].items()}))


def parent(no_wait):
    loop = load_loop()
    if not no_wait:
        say("waiting for sweep held-out scoring ('heldout done')")
        while "heldout done" not in loop.remote(f"tail -3 {SWEEP}/run_heldout.out 2>/dev/null; true", check=False):
            time.sleep(120)
    say("held-out scoring finished; serving u062")
    loop.keep_tunnel()
    model = loop.start_servers(U062)
    assert model == loop.CONFIG["lora_name"], model
    procs = {arm: subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--arm", arm], cwd=HERE,
                                   stdout=open(HERE / f"arm-{arm}.out", "a"), stderr=subprocess.STDOUT) for arm in ARMS}
    say(f"arms started: { {a: p.pid for a, p in procs.items()} }")
    codes = {arm: p.wait() for arm, p in procs.items()}
    say(f"arms finished: {codes}")
    loop.stop_servers()
    r = subprocess.run([sys.executable, str(HERE / "judge_pair.py")], cwd=HERE, capture_output=True, text=True)
    (HERE / "verdict.txt").write_text(r.stdout + r.stderr)
    say("verdict written:\n" + r.stdout[-3000:] + r.stderr[-1000:])
    say("pair done")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--arm", choices=tuple(ARMS))
    ap.add_argument("--no-wait", action="store_true")
    a = ap.parse_args()
    child(a.arm) if a.arm else parent(a.no_wait)
