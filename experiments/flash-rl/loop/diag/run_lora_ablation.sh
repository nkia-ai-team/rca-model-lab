#!/bin/bash
# 2026-10-26 (user "응 그렇게해"): after u62's regular eval, pause the loop, serve u62's LoRA plus its expert-only and dense-only
# copies on gpu1, re-score recent rollout turns under each, then release the loop. Log: diag/lora_ablation.log
cd /home/swlee/projects/rca-model-lab-mcp/outputs/flash-rl-loop-20261015 || exit 1
KEY="/home/swlee/projects/kt-key/id_container (3)"; R=work@proxy1.ainexus.ktcloud.com
rsh() { ssh -i "$KEY" -p 10569 -o BatchMode=yes $R "$@"; }
PY=/home/swlee/projects/rca-model-lab-mcp/.venv/bin/python
log() { echo "$(date -Is) $*"; }
until grep -q "u62: released" loop.log; do sleep 60; done
touch PAUSE; log "u62 released; PAUSE set (takes effect after the regular eval)"
until grep -q "paused at phase boundary" <(sed -n '/u62: released/,$p' loop.log); do sleep 30; done
log "loop paused"
SRC=/home/work/models/rca-adapters-vllm/flash-rl-loop-20261015/u062; OUT=/home/work/diag-lora-ablate/u062
scp -i "$KEY" -P 10569 -o BatchMode=yes diag/ablate_lora.py $R:/tmp/ablate_lora.py
rsh "/home/work/models/qwen-flash-next-mtp4-runtime-20261014/venv/bin/python /tmp/ablate_lora.py $SRC $OUT" || { log "ablation failed"; rm -f PAUSE; exit 1; }
CMD=$($PY -c "
import json;c=json.load(open('config.json'));s=[x for x in c['servers'] if x['gpu']==1][0]
lora='--lora-modules policy=$SRC experts=$OUT/experts dense=$OUT/dense'
print(c['serve_command'].format(gpu=1,port=s['port'],lora=lora,log=c['remote_work']+'/server-gpu1.log'))")
rsh "pkill -f 'vllm serve.*--port 18242' ; sleep 20; cd /home/work/flash-rl-loop-20261015 && setsid nohup bash -c $(printf '%q' "$CMD") > /dev/null 2>&1 < /dev/null &"
log "gpu1 relaunched with policy/experts/dense"
for i in $(seq 1 60); do
  m=$(curl -s -m 5 127.0.0.1:18242/v1/models 2>/dev/null)
  echo "$m" | grep -q '"dense"' && break; sleep 20
done
echo "$m" | grep -q '"dense"' || { log "gpu1 did not come up with the variants"; exit 1; }
for name in policy experts dense; do
  $PY diag/logprob_shift.py --updates u060,u061,u062 --turns-per-episode 1 --port 18242 --policy $name --out diag/ablation_$name.json | grep AGG | sed "s/^/$name /"
done
rm -f PAUSE; log "PAUSE removed (gpu1 keeps serving policy=u062; the loop restarts both servers at the next release)"
