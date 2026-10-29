#!/bin/bash
# 2026-10-26 learnability step 1 (static update path): one group step on u063 f01-p, log-prob change vs advantage at lr 1e-4
# (configured) and 1e-3 (amplified, diagnostic only). Output: /home/work/bench-fast19/direction-*.txt
set -u
B=/home/work/bench-fast19; mkdir -p $B; cd /home/work/flash-rl-quant-probe-20261014/general-update-fast19-20261026
PYBIN=/home/work/models/qwen-flash-next-mtp4-runtime-20261014/venv/bin/python
export CUDA_VISIBLE_DEVICES=0,1 HF_HUB_OFFLINE=1 OMP_NUM_THREADS=8 PYTHONPATH=/home/work/models/flash-rl-kernels-20261014/python PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
ARGS="--groups /home/work/flash-rl-loop-20261015/u063/groups/case-f01-p-v3-20bfac59.json --policy-version adapter-0f6f582d901317d0
 --start-adapter /home/work/models/rca-adapters/flash-rl-loop-20261015/u062/adapter.pt --checkpoint /var/tmp/flash-ckpt-local/qwen-flash-next-w4a16-autoround-4c67bf68
 --repo-src $PWD/src --preparation /home/work/flash-rl-quant-probe-20261014/data-v5/preparation-v5-20261023.json
 --split-source /home/work/flash-rl-quant-probe-20261014/data-v5/split-v5-20261023.yaml --tree --per-group-steps --direction-test"
for lr in 0.0001 0.001; do
  rm -rf $B/out-dir-$lr
  echo "=== direction lr $lr $(date -Is)"
  $PYBIN -u general_update_runner.py $ARGS --learning-rate $lr --output $B/out-dir-$lr > $B/direction-$lr.txt 2>&1
  grep -E '^\{"direction_test"|Error|error' $B/direction-$lr.txt | tail -2 | cut -c1-1500
done
echo "direction done $(date -Is)"
