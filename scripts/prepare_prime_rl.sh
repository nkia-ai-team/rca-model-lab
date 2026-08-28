#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 || "$1" != /* ]]; then
  echo "usage: $0 /absolute/path/to/prime-rl" >&2
  exit 2
fi

target=$1
revision=95734aa1dd3de26afee31e99b7b63b86ad8f4a2e
project_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
patch_file="$project_root/integrations/prime_rl/patches/0001-muse-glimmer-sft-lora.patch"

if [[ ! -d "$target/.git" ]]; then
  if [[ -e "$target" ]]; then
    echo "target exists but is not a Git checkout: $target" >&2
    exit 1
  fi
  git clone https://github.com/PrimeIntellect-ai/prime-rl.git "$target"
fi

if [[ -n "$(git -C "$target" status --porcelain)" ]]; then
  if git -C "$target" apply --reverse --check "$patch_file" >/dev/null 2>&1; then
    echo "Prime-RL Muse patch is already applied: $target"
    exit 0
  fi
  echo "refusing to modify a dirty Prime-RL checkout: $target" >&2
  exit 1
fi

git -C "$target" fetch origin "$revision"
if [[ "$(git -C "$target" rev-parse HEAD)" != "$revision" ]]; then
  git -C "$target" checkout --detach "$revision"
fi
# Upstream records three public submodules with SSH URLs. Use HTTPS so cloud
# containers do not require a personal GitHub SSH key.
git -C "$target" config submodule.prime-envs.url https://github.com/PrimeIntellect-ai/prime-envs.git
git -C "$target" config submodule.renderers.url https://github.com/PrimeIntellect-ai/renderers.git
git -C "$target" config submodule.verifiers.url https://github.com/PrimeIntellect-ai/verifiers.git
git -C "$target" submodule update --init --recursive

if git -C "$target" apply --reverse --check "$patch_file" >/dev/null 2>&1; then
  echo "Prime-RL Muse patch is already applied: $target"
  exit 0
fi

git -C "$target" apply --check "$patch_file"
git -C "$target" apply "$patch_file"
echo "Prepared patched Prime-RL checkout: $target"
