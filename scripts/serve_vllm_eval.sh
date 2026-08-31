#!/usr/bin/env bash
set -euo pipefail

model_path=${1:?usage: serve_vllm_eval.sh BASE_MODEL_PATH [PORT] [SERVED_MODEL_NAME] [LORA_ADAPTER_PATH]}
port=${2:-8002}
served_model=${3:-rca-actor}
lora_adapter=${4:-}
vllm_bin=${VLLM_BIN:-/home/work/venv-vllm/bin/vllm}
max_num_seqs=${VLLM_MAX_NUM_SEQS:-8}
max_num_batched_tokens=${VLLM_MAX_NUM_BATCHED_TOKENS:-16384}
gpu_memory_utilization=${VLLM_GPU_MEMORY_UTILIZATION:-0.95}
tokenizer_path=${VLLM_TOKENIZER_PATH:-}
structured_outputs_config=${VLLM_STRUCTURED_OUTPUTS_CONFIG:-'{"backend":"guidance","disable_any_whitespace":true}'}

test -d "$model_path"
test -x "$vllm_bin"

lora_args=()
tokenizer_args=()
if [[ -n "$tokenizer_path" ]]; then
  test -d "$tokenizer_path"
  tokenizer_args=(--tokenizer "$tokenizer_path")
fi
base_served_model="$served_model"
if [[ -n "$lora_adapter" ]]; then
  test -d "$lora_adapter"
  test -f "$lora_adapter/adapter_config.json"
  base_served_model="${served_model}-base"
  lora_args=(
    --enable-lora
    --max-lora-rank "${VLLM_MAX_LORA_RANK:-8}"
    --lora-target-modules q_proj k_proj v_proj o_proj up_proj down_proj gate_proj
    --lora-modules "${served_model}=${lora_adapter}"
  )
fi

# guidance is pinned because the xgrammar version on the KT evaluator rejects
# Muse-Glimmer's valid end-of-turn token (200008) after otherwise-valid JSON.
# All baseline/SFT/RL stages must use this same launch contract.
export FLASHINFER_DISABLE_VERSION_CHECK=${FLASHINFER_DISABLE_VERSION_CHECK:-1}
# KT containers expose a mutable user site that can shadow the isolated vLLM
# environment with an incompatible Transformers/tokenizers pair.
export PYTHONNOUSERSITE=1
unset PYTHONPATH
exec "$vllm_bin" serve "$model_path" \
  "${tokenizer_args[@]}" \
  --served-model-name "$base_served_model" \
  --host 127.0.0.1 \
  --port "$port" \
  --max-model-len 32768 \
  --gpu-memory-utilization "$gpu_memory_utilization" \
  --max-num-seqs "$max_num_seqs" \
  --max-num-batched-tokens "$max_num_batched_tokens" \
  --enable-chunked-prefill \
  --enable-prefix-caching \
  --language-model-only \
  "${lora_args[@]}" \
  --structured-outputs-config "$structured_outputs_config"
