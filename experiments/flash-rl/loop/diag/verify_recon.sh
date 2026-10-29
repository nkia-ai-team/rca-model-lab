#!/bin/bash
# 2026-10-27: reproduce the 10-26 baseline bench on the reconstructed fast19 runtime (NFS) — compare tree stats (deterministic),
# episode time, peak memory, grad norm at the bench step and the optimizer settings with the 10-26 run.
set -u
B=/home/work/models/bench-recon-20261027; mkdir -p $B; cd /home/work/flash-rl-quant-probe-20261014/general-update-fast19-20261026
PYBIN=/home/work/models/qwen-flash-next-mtp4-runtime-20261014/venv/bin/python
export CUDA_VISIBLE_DEVICES=0,1 HF_HUB_OFFLINE=1 OMP_NUM_THREADS=8 PYTHONPATH=/home/work/models/flash-rl-kernels-20261014/python PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
$PYBIN -u general_update_runner.py --groups /home/work/flash-rl-loop-20261015/u063/groups/case-f01-p-v3-20bfac59.json \
  --policy-version adapter-0f6f582d901317d0 --start-adapter /home/work/models/rca-adapters/flash-rl-loop-20261015/u062/adapter.pt \
  --checkpoint /home/work/models/qwen-flash-next-w4a16-autoround-4c67bf68 --repo-src $PWD/src \
  --preparation /home/work/flash-rl-quant-probe-20261014/data-v5/preparation-v5-20261023.json \
  --split-source /home/work/flash-rl-quant-probe-20261014/data-v5/split-v5-20261023.yaml --learning-rate 0.0001 --tree --per-group-steps \
  --groups-per-step 2 --max-episodes 2 --bench-step-after 1 --output $B/out-base --speed "" --save-grad $B/grad-base.pt --save-params $B/params-base.pt > $B/log-base.txt 2>&1
echo "verify done $(date -Is)"
