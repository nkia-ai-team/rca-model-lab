#!/bin/bash
# 2026-10-27: per-flag speed/memory/equivalence runs on the verified reconstructed fast19 (the 10-26 per-flag results were lost
# with the KT session). Reference = base2 of verify_round2 (same args). Output on NFS: /home/work/models/bench-recon-20261027/
B=/home/work/models/bench-recon-20261027
cd /home/work/flash-rl-quant-probe-20261014/general-update-fast19-20261026 || exit 1
RT=$(pwd)
PYBIN=/home/work/models/qwen-flash-next-mtp4-runtime-20261014/venv/bin/python
export CUDA_VISIBLE_DEVICES=0,1 HF_HUB_OFFLINE=1 OMP_NUM_THREADS=8 PYTHONPATH=/home/work/models/flash-rl-kernels-20261014/python PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
ARGS=(--groups /home/work/flash-rl-loop-20261015/u063/groups/case-f01-p-v3-20bfac59.json --policy-version adapter-0f6f582d901317d0
      --start-adapter /home/work/models/rca-adapters/flash-rl-loop-20261015/u062/adapter.pt
      --checkpoint /home/work/models/qwen-flash-next-w4a16-autoround-4c67bf68 --repo-src "$RT/src"
      --preparation /home/work/flash-rl-quant-probe-20261014/data-v5/preparation-v5-20261023.json
      --split-source /home/work/flash-rl-quant-probe-20261014/data-v5/split-v5-20261023.yaml
      --learning-rate 0.0001 --tree --per-group-steps --groups-per-step 2 --max-episodes 2 --bench-step-after 1)
for variant in "stack:stack" "chunk:chunk" "empty:empty" "gpustore:gpustore" "nostore:empty,chunk,stack" "all:empty,chunk,stack,gpustore"; do
  name=${variant%%:*}; flags=${variant#*:}
  rm -rf "$B/out-$name"
  echo "=== $name ($flags) $(date -Is)"
  "$PYBIN" -u general_update_runner.py "${ARGS[@]}" --speed "$flags" --output "$B/out-$name" \
      --save-grad "$B/grad-$name.pt" --save-params "$B/params-$name.pt" > "$B/log-$name.txt" 2>&1
  grep -E '^\{"bench"|bench step|Error|Traceback' "$B/log-$name.txt" | tail -3 | cut -c1-300
done
echo "flags done $(date -Is)"
