#!/bin/bash
# 2026-10-23: wait for milestone 39 in state.json, then switch config to data v5 (apply_v5.py). Log: data-v5/apply_v5.log
cd /home/swlee/projects/rca-model-lab-mcp/outputs/flash-rl-loop-20261015 || exit 1
while true; do
  if python3 -c "import json,sys;s=json.load(open('state.json'));sys.exit(0 if any(m.get('at')==39 for m in s.get('milestone_evals',[])) else 1)"; then
    sleep 60
    /home/swlee/projects/rca-model-lab-mcp/.venv/bin/python data-v5/apply_v5.py && echo "$(date -Is) applied" || echo "$(date -Is) apply FAILED"
    exit 0
  fi
  pgrep -f 'outputs/flash-rl-loop-20261015/loop.py' >/dev/null || { echo "$(date -Is) loop dead, not applying"; exit 1; }
  sleep 120
done
