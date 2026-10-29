"""External watchdog for the Flash RL loop (2026-10-15, after a silent runner exit left the loop waiting ~2 h).

Checks every INTERVAL seconds and EXITS (printing why) on the first anomaly or notable event, so the supervising
session is woken up immediately; it is re-armed after handling. Read-only: it never touches the loop or the GPUs.

Anomalies: loop process dead; new Traceback in loop.out; during training the runner is gone while progress is not
terminal, or progress.json has not changed for STALL_MIN minutes; outside training (and not paused) loop.log has not
changed for STALL_MIN minutes; /home/work on KT below MIN_FREE_GB.
Events: update released / no update / rollback / pause / resume / runner-exit lines in loop.log.
"""
import json
import re
import subprocess
import sys
import time
from pathlib import Path

W = Path(__file__).resolve().parent
CONFIG = json.loads((W / "config.json").read_text())
SSH = ["ssh", "-q", "-i", CONFIG["kt"]["key"], "-p", str(CONFIG["kt"]["port"]), "-o", "BatchMode=yes", "-o", "ConnectTimeout=30",
       "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null", CONFIG["kt"]["host"]]
INTERVAL, STALL_MIN, MIN_FREE_GB, MAX_HOURS = 120, 75, 5, 12
EVENT = re.compile(r"released ->|no update|rollback|paused at phase|pausing with GPUs free|resumed|runner exited without|"
                   r"replay update|replay of .* dropped|carried groups .* dropped|every group flat|candidate .* failed|discarded stale prefetch")


def loop_alive():
    out = subprocess.run(["pgrep", "-f", "rca-model-lab-mcp/.venv/bin/python outputs/flash-rl-loop-20261015/loop.py"],
                         capture_output=True, text=True).stdout.split()
    return bool(out)


def remote(cmd):
    try:
        return subprocess.run(SSH + [cmd], capture_output=True, text=True, timeout=120).stdout
    except subprocess.TimeoutExpired:
        return ""


def main():
    log, out = W / "loop.log", W / "loop.out"
    offset = log.stat().st_size
    tracebacks = out.read_text(errors="replace").count("Traceback")
    gone = 0
    t_end = time.time() + MAX_HOURS * 3600
    while time.time() < t_end:
        time.sleep(INTERVAL)
        if not loop_alive():
            return f"ANOMALY loop process is not running"
        text = log.read_text(errors="replace")
        new = text[offset:]
        offset = len(text)
        hits = [l for l in new.splitlines() if EVENT.search(l)]
        if hits:
            return "EVENT\n" + "\n".join(hits[-6:])
        tb = out.read_text(errors="replace").count("Traceback")
        if tb > tracebacks:
            return "ANOMALY new Traceback in loop.out:\n" + out.read_text(errors="replace")[-1500:]
        paused = (W / "PAUSE").exists()
        state = json.loads((W / "state.json").read_text())
        cur = state.get("current") or {}
        now = time.time()
        if cur.get("phase") == "train" and not paused:
            k = cur["k"]
            o = f"{CONFIG['adapter_root']}/u{k:03d}"
            r = remote(f"pgrep -f '[g]eneral_update_runner.py --groups {CONFIG['remote_work']}/u{k:03d}/groups' >/dev/null && echo ALIVE || echo GONE; "
                       f"stat -c %Y {o}/progress.json 2>/dev/null || echo 0; date +%s; "
                       f"python3 -c \"import json;print(json.load(open('{o}/progress.json')).get('status'))\" 2>/dev/null || echo none; "
                       f"df --output=avail -B1G /home/work | tail -1").split()
            if len(r) >= 5:
                alive, mtime, rnow, status, free = r[0] == "ALIVE", int(r[1]), int(r[2]), r[3], int(r[4])
                gone = 0 if alive or status in ("passed", "blocked_drift_budget", "failed") else gone + 1
                if gone >= 2:
                    return f"ANOMALY u{k} runner gone while progress status is {status!r}"
                if mtime and rnow - mtime > STALL_MIN * 60:
                    return f"ANOMALY u{k} progress.json unchanged for {(rnow - mtime) // 60} min (status {status})"
                if free < MIN_FREE_GB:
                    return f"ANOMALY KT /home/work free {free} GB"
        elif not paused:
            last = max(log.stat().st_mtime, (W / "state.json").stat().st_mtime)   # the loop writes state at every phase step
            # long evals (milestone: 113 cases x 2 policies) write no loop.log line for hours; each finished case leaves a log
            last = max([last] + [f.stat().st_mtime for f in (W / "eval").glob("*/*/*.log")])
            if now - last > STALL_MIN * 60:
                return f"ANOMALY no loop activity for {int(now - last) // 60} min (phase {cur.get('phase')})"
    return "TIMEOUT watchdog reached its maximum runtime; re-arm"


if __name__ == "__main__":
    print(time.strftime("%H:%M:%S"), main(), flush=True)
