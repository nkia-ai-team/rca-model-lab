#!/bin/bash
# 2026-10-28 lr/optimizer sweep (user: "목표설정해놓고 최적 학습 상태 찾고 그상태로 빨리 학습"). Each arm = one production-like
# update from u062 (= u063 recipe; groups must be sampled by the start policy or its parent) (fast20 + stack, per-group steps, 1 group/step -> 4 steps) on the same 4 mixed-outcome groups. Arms are then
# compared on held-out groups (heldout_score.py). Output: /home/work/models/sweep-20261028/arm-*/
S=/home/work/models/sweep-20261028
cd /home/work/flash-rl-quant-probe-20261014/general-update-fast20-20261027 || exit 1
RT=$(pwd)
PYBIN=/home/work/models/qwen-flash-next-mtp4-runtime-20261014/venv/bin/python
export CUDA_VISIBLE_DEVICES=0,1 HF_HUB_OFFLINE=1 OMP_NUM_THREADS=8 PYTHONPATH=/home/work/models/flash-rl-kernels-20261014/python PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
ARGS=(--groups $S/train/*.json --policy-version adapter-0f6f582d901317d0
      --start-adapter /home/work/models/rca-adapters/flash-rl-loop-20261015/u062/adapter.pt
      --checkpoint /home/work/models/qwen-flash-next-w4a16-autoround-4c67bf68 --repo-src "$RT/src"
      --preparation /home/work/flash-rl-quant-probe-20261014/data-v5/preparation-v5-20261023.json
      --split-source /home/work/flash-rl-quant-probe-20261014/data-v5/split-v5-20261023.yaml
      --tree --per-group-steps --speed stack --checkpoint-every 10)
for arm in ${ARMS:-"adamw:0.001" "adamw:0.0003" "muon:0.0003" "adamw:0.0001"}; do
  opt=${arm%%:*}; lr=${arm#*:}; name=arm-$opt-$lr; out=$S/$name
  [ -f $out/adapter.pt ] && { echo "skip $name (done)"; continue; }
  echo "=== $name $(date -Is)"
  "$PYBIN" -u general_update_runner.py "${ARGS[@]}" --optimizer $opt --learning-rate $lr --output $out > $S/log-$name.txt 2>&1
  grep -E '"status"|Error|Traceback' $S/log-$name.txt | tail -2 | cut -c1-300
  [ -f $out/adapter.pt ] && (cd /home/work/flash-rl-quant-probe-20261014/lora-export-20261015 && "$PYBIN" export_vllm_lora.py $out/adapter.pt $S/lora-$name > $S/export-$name.txt 2>&1; tail -1 $S/export-$name.txt)
done
echo "sweep train done $(date -Is)"
