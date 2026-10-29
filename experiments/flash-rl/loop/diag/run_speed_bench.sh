#!/bin/bash
# 2026-10-26 speed benchmark on KT (GPUs must be free), revised after Codex review:
#   timing runs WITHOUT the profiler: base / all / base2 — two episodes of u063 f01-p with a real optimizer step after the
#   first (exercises flush/refresh of stacked expert leaves), saving parameters after the step and the next episode's gradient;
#   one separate py-spy profile run of base (one episode). Strict per-family comparison in bench_compare.py.
# Output: /home/work/bench-fast19/
set -u
B=/home/work/bench-fast19; mkdir -p $B; cd /home/work/flash-rl-quant-probe-20261014/general-update-fast19-20261026
PYBIN=/home/work/models/qwen-flash-next-mtp4-runtime-20261014/venv/bin/python
export CUDA_VISIBLE_DEVICES=0,1 HF_HUB_OFFLINE=1 OMP_NUM_THREADS=8 PYTHONPATH=/home/work/models/flash-rl-kernels-20261014/python PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
ARGS="--groups /home/work/flash-rl-loop-20261015/u063/groups/case-f01-p-v3-20bfac59.json --policy-version adapter-0f6f582d901317d0
 --start-adapter /home/work/models/rca-adapters/flash-rl-loop-20261015/u062/adapter.pt --checkpoint /var/tmp/flash-ckpt-local/qwen-flash-next-w4a16-autoround-4c67bf68
 --repo-src $PWD/src --preparation /home/work/flash-rl-quant-probe-20261014/data-v5/preparation-v5-20261023.json
 --split-source /home/work/flash-rl-quant-probe-20261014/data-v5/split-v5-20261023.yaml --learning-rate 0.0001 --tree --per-group-steps --groups-per-step 2"
for variant in "base:" "all:empty,chunk,stack,gpustore" "base2:" ${EXTRA_VARIANTS:-}; do
  name=${variant%%:*}; flags=${variant#*:}
  rm -rf $B/out-$name
  echo "=== $name ($flags) $(date -Is)"
  $PYBIN -u general_update_runner.py $ARGS --max-episodes 2 --bench-step-after 1 --output $B/out-$name --speed "$flags" \
      --save-grad $B/grad-$name.pt --save-params $B/params-$name.pt > $B/log-$name.txt 2>&1
  grep -E '^\{"bench"|bench step|episode g|Error|error' $B/log-$name.txt | tail -4 | cut -c1-400
done
echo "=== profile base $(date -Is)"
rm -rf $B/out-prof
/tmp/py-spy record -r 20 -f raw -o $B/pyspy-base.raw -- $PYBIN -u general_update_runner.py $ARGS --max-episodes 1 --output $B/out-prof > $B/log-prof.txt 2>&1
grep -E 'episode g|Error' $B/log-prof.txt | tail -2 | cut -c1-200
$PYBIN /tmp/bench_compare.py
echo "done $(date -Is)"
