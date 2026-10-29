#!/bin/bash
# 2026-10-23 v2: hold the loop at the first phase boundary after milestone 39 (PAUSE), switch config + loop.py (apply_v5.py), then
# release it, so the re-exec runs the milestone repair before u48 instead of after a whole rollout. Log: data-v5/apply_v5.log
cd /home/swlee/projects/rca-model-lab-mcp/outputs/flash-rl-loop-20261015 || exit 1
touch PAUSE && echo "$(date -Is) PAUSE set (takes effect at the first phase boundary after the milestone)"
while true; do
  if python3 -c "import json,sys;s=json.load(open('state.json'));sys.exit(0 if any(m.get('at')==39 for m in s.get('milestone_evals',[])) else 1)"; then
    until grep -q "paused at phase boundary" <(tail -5 loop.log); do sleep 10; done
    /home/swlee/projects/rca-model-lab-mcp/.venv/bin/python data-v5/apply_v5.py && echo "$(date -Is) applied" || echo "$(date -Is) apply FAILED"
    rm -f PAUSE && echo "$(date -Is) PAUSE removed"
    exit 0
  fi
  pgrep -f 'outputs/flash-rl-loop-20261015/loop.py' >/dev/null || { echo "$(date -Is) loop dead, not applying"; rm -f PAUSE; exit 1; }
  sleep 60
done
