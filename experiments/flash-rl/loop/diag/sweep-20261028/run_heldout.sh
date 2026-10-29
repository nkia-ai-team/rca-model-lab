#!/bin/bash
# 2026-10-28 sweep phase 2: wait for the arm trainings, serve start(u062)+arms on both GPUs, held-out transfer score, stop servers.
S=/home/work/models/sweep-20261028; cd $S
until grep -q "sweep train done" sweep_train.out; do sleep 60; done
LORAS=(); NAMES=()
for d in $S/lora-arm-*; do [ -f $d/adapter_config.json ] || continue; n=${d##*/lora-}; LORAS+=("$n=$d"); NAMES+=("$n"); done
echo "arms: ${NAMES[*]} $(date -Is)"
PIDS=()
for gpu in 0 1; do
  setsid nohup bash $S/serve.sh $gpu $((18241+gpu)) $S/server-gpu$gpu.log "${LORAS[@]}" > /dev/null 2>&1 < /dev/null &
  PIDS+=($!)
done
for i in $(seq 1 90); do
  ok=0; for port in 18241 18242; do curl -s -m 5 127.0.0.1:$port/v1/models | grep -q '"start"' && ok=$((ok+1)); done
  [ $ok = 2 ] && break; sleep 20
done
echo "servers ready ($ok/2) $(date -Is)"
/usr/bin/python3 -u $S/heldout_score.py --heldout $S/heldout/*.json --probe $S/probe/*.json --models start "${NAMES[@]}" --out $S/heldout-score.json > $S/heldout-score.log 2>&1
grep -E "^(heldout|probe) " $S/heldout-score.log | cut -c1-3000
for p in "${PIDS[@]}"; do kill -- -$p 2>/dev/null; done
sleep 30; nvidia-smi --query-gpu=index,memory.used --format=csv,noheader
echo "heldout done $(date -Is)"
