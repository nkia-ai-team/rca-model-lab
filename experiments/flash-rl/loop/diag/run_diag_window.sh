#!/bin/bash
# 2026-10-26 (user "u63 반영 끝나면 루프 멈추고 1단계 학습가능성 진단 진행해. 속도 시험도 그 창에서 같이 해"):
# wait for the PAUSE_AFTER=63 hold, free the KT GPUs, run the speed benchmark then the direction test. The loop stays
# paused afterwards (step 1 continues). Log: diag/diag_window.log
cd /home/swlee/projects/rca-model-lab-mcp/outputs/flash-rl-loop-20261015 || exit 1
KEY="/home/swlee/projects/kt-key/id_container (3)"; R=work@proxy1.ainexus.ktcloud.com
rsh() { ssh -i "$KEY" -p 10569 -o BatchMode=yes $R "$@"; }
log() { echo "$(date -Is) $*"; }
until grep -q "update u63 finished: pausing with GPUs free" loop.log; do sleep 60; done
log "loop held after u63"
scp -i "$KEY" -P 10569 -o BatchMode=yes diag/run_speed_bench.sh diag/bench_compare.py diag/run_direction_test.sh diag/test_stack_multistep.py $R:/tmp/
scp -i "$KEY" -P 10569 -o BatchMode=yes diag/stop_vllm.sh $R:/tmp/stop_vllm.sh
rsh "bash /tmp/stop_vllm.sh"
rsh "chmod +x /tmp/run_speed_bench.sh /tmp/run_direction_test.sh; setsid nohup bash -c 'CUDA_VISIBLE_DEVICES=0 /home/work/models/qwen-flash-next-mtp4-runtime-20261014/venv/bin/python /tmp/test_stack_multistep.py; /tmp/run_speed_bench.sh; /tmp/run_direction_test.sh' > /home/work/diag-window.out 2>&1 < /dev/null &"
log "speed bench + direction test started"
until rsh "grep -q '^direction done ' /home/work/diag-window.out"; do sleep 120; done
rsh "cat /home/work/diag-window.out"
log "window work finished; loop stays paused for step 1"
