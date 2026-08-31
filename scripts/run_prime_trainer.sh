#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 2 || "$1" != /* || "$2" != /* ]]; then
  echo "usage: $0 /absolute/path/to/prime-rl /absolute/path/to/trainer.toml" >&2
  exit 2
fi

prime_checkout=$1
trainer_config=$2
torchrun="$prime_checkout/.venv/bin/torchrun"

if [[ ! -x "$torchrun" ]]; then
  echo "Prime trainer torchrun is not executable: $torchrun" >&2
  exit 1
fi
if [[ ! -f "$trainer_config" ]]; then
  echo "trainer config does not exist: $trainer_config" >&2
  exit 1
fi

cd "$prime_checkout"
exec "$torchrun" \
  --standalone \
  --nnodes=1 \
  --nproc-per-node=1 \
  -m prime_rl.entrypoints.trainer \
  @ "$trainer_config"
