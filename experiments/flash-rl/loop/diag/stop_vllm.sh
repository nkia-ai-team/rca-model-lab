#!/bin/bash
# Stop the loop's vLLM servers by process structure (python .../bin/vllm serve ... --port 18241|18242), never by a pattern
# that also matches the calling shell.
ps -eo pid,args | awk '$2 ~ /python$/ && $3 ~ /bin\/vllm$/ && $4 == "serve" {for (i = 5; i < NF; i++) if ($i == "--port" && ($(i+1) == "18241" || $(i+1) == "18242")) print $1}' | xargs -r kill
sleep 30
nvidia-smi --query-gpu=index,memory.used --format=csv,noheader
