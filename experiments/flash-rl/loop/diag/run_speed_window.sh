#!/bin/bash
# 2026-10-26: GPU window for the speed benchmark. Pause the loop once u64's rollout is underway (it holds at the boundary
# before training), free the KT GPUs, run diag/run_speed_bench.sh there, then release the loop (training needs no servers).
# Log: diag/speed_window.log
cd /home/swlee/projects/rca-model-lab-mcp/outputs/flash-rl-loop-20261015 || exit 1
KEY="/home/swlee/projects/kt-key/id_container (3)"; R=work@proxy1.ainexus.ktcloud.com
rsh() { ssh -i "$KEY" -p 10569 -o BatchMode=yes $R "$@"; }
log() { echo "$(date -Is) $*"; }
until grep -q "u64: rolling" loop.log; do sleep 60; done
touch PAUSE; log "u64 rollout started; PAUSE set"
until grep -q "paused at phase boundary" <(sed -n '/u64: rolling/,$p' loop.log); do sleep 30; done
log "loop paused before u64 training"
scp -i "$KEY" -P 10569 -o BatchMode=yes diag/run_speed_bench.sh diag/bench_compare.py $R:/tmp/
rsh "pkill -f 'vllm serve.*--port 1824[12]'; sleep 30; nvidia-smi --query-gpu=index,memory.used --format=csv,noheader"
rsh "chmod +x /tmp/run_speed_bench.sh; setsid nohup /tmp/run_speed_bench.sh > /home/work/bench-fast19.out 2>&1 < /dev/null &"
log "bench started"
until rsh "grep -q '^done ' /home/work/bench-fast19.out"; do sleep 60; done
rsh "cat /home/work/bench-fast19.out"
rm -f PAUSE; log "PAUSE removed"
