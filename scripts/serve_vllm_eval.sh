#!/usr/bin/env bash
set -euo pipefail

model_path=${1:?usage: serve_vllm_eval.sh MODEL_PATH [PORT] [SERVED_MODEL_NAME]}
port=${2:-8002}
served_model=${3:-rca-actor}
vllm_bin=${VLLM_BIN:-/home/work/venv-vllm/bin/vllm}
max_num_seqs=${VLLM_MAX_NUM_SEQS:-3}

test -d "$model_path"
test -x "$vllm_bin"

# guidance is pinned because the xgrammar version on the KT evaluator rejects
# Muse-Glimmer's valid end-of-turn token (200008) after otherwise-valid JSON.
# All baseline/SFT/RL stages must use this same launch contract.
export FLASHINFER_DISABLE_VERSION_CHECK=${FLASHINFER_DISABLE_VERSION_CHECK:-1}
exec "$vllm_bin" serve "$model_path" \
  --served-model-name "$served_model" \
  --host 127.0.0.1 \
  --port "$port" \
  --max-model-len 32768 \
  --gpu-memory-utilization 0.95 \
  --max-num-seqs "$max_num_seqs" \
  --enable-prefix-caching \
  --structured-outputs-config '{"backend":"guidance"}'
