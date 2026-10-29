#!/bin/bash
# 2026-10-27 reconstruction check round 2 (warm caches): repeat the stepped baseline bench (grad norm reproducibility on the new
# host) and repeat the per-turn dump (reconstructed-vs-reconstructed noise floor for the u063 drift comparison).
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
      --learning-rate 0.0001 --tree --per-group-steps --groups-per-step 2 --speed "")
rm -rf "$B/out-base2" "$B/out-rows2"
echo "start $(date -Is)"
"$PYBIN" -u general_update_runner.py "${ARGS[@]}" --max-episodes 2 --bench-step-after 1 --output "$B/out-base2" \
    --save-grad "$B/grad-base2.pt" --save-params "$B/params-base2.pt" > "$B/log-base2.txt" 2>&1
"$PYBIN" -u general_update_runner.py "${ARGS[@]}" --max-episodes 1 --output "$B/out-rows2" --dump-rows "$B/rows-e0-2.json" > "$B/log-rows2.txt" 2>&1
echo "round2 done $(date -Is)"
